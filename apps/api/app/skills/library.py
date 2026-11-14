"""Skill libraries on GitHub: browse a repository's Claude-format skills and fetch one (docs/SKILLS.md).

A library is any public GitHub repository holding skill folders (a folder with ``SKILL.md``). Lumio talks only to
``api.github.com`` and ``raw.githubusercontent.com``; the owner and repository come from a strict pattern, so a pasted
link can't point the server anywhere else. Every import is pinned to the commit it was read from, so what was reviewed
is what is stored, and "check for updates" compares that commit with the branch head.

Anonymous GitHub reads allow 60 API calls an hour per server address. A listing costs two calls and is cached for
ten minutes; skill files come from raw.githubusercontent.com, which doesn't count. Set SKILLS_GITHUB_TOKEN (a token
with no scopes is enough) to raise the limit to 5,000.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
import time
from urllib.parse import quote

import httpx

from app.skills import claude_format as cf

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
TIMEOUT = 20.0
CACHE_SECONDS = 600
MAX_CATALOG = 80  # skills listed from one repository
FETCH_CONCURRENCY = 8

FEATURED = [
    {
        "id": "anthropic",
        "name": "Anthropic skills",
        "url": "https://github.com/anthropics/skills",
        "about": "Anthropic's public skills: documents (Word, Excel, PowerPoint, PDF), design, testing, MCP servers and more.",
    },
    {
        "id": "trinity",
        "name": "Trinity skills",
        "url": "https://github.com/abilityai/trinity-skills",
        "about": "Ability.ai's library for Trinity agents: project management, one-pagers, research, images and workspace hygiene.",
    },
]

_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?(?:/(?:tree|blob)/(?P<ref>[A-Za-z0-9._/-]{1,200}?)(?:/(?P<path>[^?#]*))?)?/?(?:[?#].*)?$"
)
_SHORT = re.compile(r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100})$")

_CACHE: dict[tuple, tuple[float, object]] = {}
_transport: httpx.AsyncBaseTransport | None = None  # tests swap in httpx.MockTransport


class LibraryError(ValueError):
    """Shown to the user as is."""


def parse_url(url: str) -> dict:
    """{owner, repo, ref, path} from a GitHub link (repository, folder or SKILL.md) or ``owner/repo``.

    A branch name containing ``/`` is read as branch + path; use the branch's short name or a tag in such links."""
    text = str(url or "").strip()
    m = _URL.match(text) or _SHORT.match(text)
    if not m:
        raise LibraryError("Paste a GitHub link, for example https://github.com/anthropics/skills.")
    parts = m.groupdict()
    ref = (parts.get("ref") or "").strip("/")
    path = (parts.get("path") or "").strip("/")
    if "/" in ref:  # tree/<branch>/<path> where our lazy match took too much: first segment is the ref
        ref, _, extra = ref.partition("/")
        path = f"{extra}/{path}".strip("/")
    if path.endswith("SKILL.md"):
        path = posixpath.dirname(path)
    if path and cf.clean_path(path) is None:
        raise LibraryError("That link points outside the repository.")
    if parts["repo"] in (".", ".."):
        raise LibraryError("That isn't a repository link.")
    return {"owner": parts["owner"], "repo": parts["repo"], "ref": ref, "path": path}


def _client() -> httpx.AsyncClient:
    from app.config import get_settings

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "lumio-skills", "X-GitHub-Api-Version": "2022-11-28"}
    token = getattr(get_settings(), "skills_github_token", "") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(timeout=TIMEOUT, headers=headers, follow_redirects=False, transport=_transport)


def _raise_for(resp: httpx.Response, what: str) -> None:
    if resp.status_code == 404:
        raise LibraryError(f"{what} wasn't found on GitHub. Check the link, and that the repository is public.")
    if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
        reset = resp.headers.get("x-ratelimit-reset", "")
        wait = ""
        if reset.isdigit():
            wait = f" Try again in about {max(1, (int(reset) - int(time.time())) // 60 + 1)} minutes."
        raise LibraryError("GitHub's limit for anonymous reads from this server is used up." + wait + " An admin can set SKILLS_GITHUB_TOKEN to raise it.")
    if resp.status_code >= 400:
        raise LibraryError(f"GitHub answered {resp.status_code} for {what}.")


def _cached(key: tuple):
    hit = _CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return None


def _store(key: tuple, value):
    if len(_CACHE) > 200:
        _CACHE.clear()
    _CACHE[key] = (time.monotonic() + CACHE_SECONDS, value)
    return value


async def resolve(src: dict, client: httpx.AsyncClient) -> dict:
    """The source pinned to a commit, with its file tree: {owner, repo, ref, sha, path, tree: [{path, size}]}."""
    owner, repo, ref = src["owner"], src["repo"], src.get("ref") or ""
    key = ("tree", owner.lower(), repo.lower(), ref)
    hit = _cached(key)
    if hit is None:
        base = f"{API}/repos/{quote(owner)}/{quote(repo)}"
        if not ref:
            resp = await client.get(base)
            _raise_for(resp, f"github.com/{owner}/{repo}")
            ref = str(resp.json().get("default_branch") or "main")
        resp = await client.get(f"{base}/commits/{quote(ref, safe='')}", headers={"Accept": "application/vnd.github.sha"})
        _raise_for(resp, f"“{ref}” in {owner}/{repo}")
        sha = resp.text.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise LibraryError("GitHub didn't return a commit for that branch or tag.")
        resp = await client.get(f"{base}/git/trees/{sha}", params={"recursive": "1"})
        _raise_for(resp, f"the files of {owner}/{repo}")
        data = resp.json()
        tree = [{"path": t["path"], "size": int(t.get("size") or 0)} for t in data.get("tree") or [] if t.get("type") == "blob"]
        hit = _store(key, {"ref": ref, "sha": sha, "tree": tree, "truncated": bool(data.get("truncated"))})
    return {"owner": owner, "repo": repo, "path": src.get("path") or "", **hit}


def _label(src: dict) -> str:
    where = f"github.com/{src['owner']}/{src['repo']}"
    return f"{where}/{src['path']}" if src.get("path") else where


def skill_roots(tree: list[dict], under: str = "") -> list[str]:
    """Folders holding a SKILL.md, inside ``under`` (the folder itself counts)."""
    prefix = f"{under}/" if under else ""
    roots = []
    for item in tree:
        path = item["path"]
        if posixpath.basename(path) != "SKILL.md":
            continue
        if under and not (path == f"{under}/SKILL.md" or path.startswith(prefix)):
            continue
        roots.append(posixpath.dirname(path))
    return sorted(set(roots), key=lambda r: (r.count("/"), r))


def _members(tree: list[dict], root: str, roots: list[str]) -> list[dict]:
    """The files of one skill folder, without the files of skill folders nested in it."""
    prefix = f"{root}/" if root else ""
    nested = [r for r in roots if r != root and (r.startswith(prefix) if prefix else True)]
    out = []
    for item in tree:
        path = item["path"]
        if prefix and not path.startswith(prefix):
            continue
        if any(path.startswith(f"{n}/") for n in nested):
            continue
        rel = path[len(prefix):]
        if rel != "SKILL.md" and cf.clean_path(rel) is not None:
            out.append({"path": rel, "size": item["size"]})
    return out


async def _raw(client: httpx.AsyncClient, src: dict, path: str, limit: int) -> bytes:
    url = f"{RAW}/{quote(src['owner'])}/{quote(src['repo'])}/{src['sha']}/{quote(path)}"
    async with client.stream("GET", url) as resp:
        _raise_for(resp, path)
        data = bytearray()
        async for chunk in resp.aiter_bytes():
            data.extend(chunk)
            if len(data) > limit:
                raise LibraryError(f"{path} is larger than {limit // 1000} KB.")
        return bytes(data)


async def catalog(url: str) -> dict:
    """The skills in a repository (or one folder of it): name, title, description, licence and file count each."""
    src = parse_url(url)
    async with _client() as client:
        pinned = await resolve(src, client)
        key = ("catalog", pinned["owner"].lower(), pinned["repo"].lower(), pinned["sha"], pinned["path"])
        hit = _cached(key)
        if hit is not None:
            return hit
        roots = skill_roots(pinned["tree"], pinned["path"])
        if not roots:
            raise LibraryError(f"No SKILL.md found in {_label(pinned)}.")
        all_roots = skill_roots(pinned["tree"])
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(root: str) -> dict:
            files = _members(pinned["tree"], root, all_roots)
            row = {
                "path": root,
                "folder": posixpath.basename(root) or pinned["repo"],
                "files": len(files),
                "size": sum(f["size"] for f in files),
                "has_scripts": any(f["path"].startswith("scripts/") or f["path"].endswith((".py", ".sh", ".js")) for f in files),
            }
            try:
                async with sem:
                    text = cf.text_of(await _raw(client, pinned, f"{root}/SKILL.md" if root else "SKILL.md", cf.MAX_SKILL_MD)) or ""
                parsed = cf.parse_skill_md(text, row["folder"])
            except (LibraryError, cf.SkillFormatError) as exc:
                return {**row, "name": row["folder"], "title": cf.humanize(row["folder"]), "description": "", "license": "", "error": str(exc)}
            return {
                **row,
                "name": parsed["name"],
                "title": cf.humanize(parsed["name"]),
                "description": parsed["description"],
                "license": parsed["license"],
                "category": str((parsed["meta"] or {}).get("category") or ""),
            }

        skills = await asyncio.gather(*(one(r) for r in roots[:MAX_CATALOG]))
    result = {
        "source": {
            "owner": pinned["owner"],
            "repo": pinned["repo"],
            "ref": pinned["ref"],
            "sha": pinned["sha"],
            "path": pinned["path"],
            "url": f"https://github.com/{pinned['owner']}/{pinned['repo']}",
            "label": _label(pinned),
        },
        "skills": sorted(skills, key=lambda s: s["title"].lower()),
        "more": max(0, len(roots) - MAX_CATALOG),
        "truncated": pinned["truncated"],
    }
    return _store(key, result)


async def fetch_skill(url: str, path: str, ref: str = "") -> dict:
    """One skill folder from a repository, pinned: {parsed, files, skipped, source}."""
    src = parse_url(url)
    if ref:
        src["ref"] = ref
    clean = cf.clean_path(path) if path else ""
    if clean is None:
        raise LibraryError("That skill path isn't valid.")
    async with _client() as client:
        pinned = await resolve({**src, "path": ""}, client)
        roots = skill_roots(pinned["tree"])
        if clean not in roots:
            raise LibraryError(f"No SKILL.md at {clean or 'the top'} of {pinned['owner']}/{pinned['repo']}.")
        members = _members(pinned["tree"], clean, roots)
        prefix = f"{clean}/" if clean else ""
        text = cf.text_of(await _raw(client, pinned, f"{prefix}SKILL.md", cf.MAX_SKILL_MD)) or ""
        parsed = cf.parse_skill_md(text, posixpath.basename(clean) or pinned["repo"])
        wanted = [m for m in members if not cf.is_binary_name(m["path"]) and m["size"] <= cf.MAX_FILE][: cf.MAX_FILES + 20]
        left_out = [{"path": m["path"], "why": "binary" if cf.is_binary_name(m["path"]) else "too large"} for m in members if m not in wanted]
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def get(m: dict) -> tuple[str, bytes]:
            async with sem:
                return m["path"], await _raw(client, pinned, f"{prefix}{m['path']}", cf.MAX_FILE)

        entries = await asyncio.gather(*(get(m) for m in wanted))
    files, skipped = cf.collect_files(list(entries))
    here = {**pinned, "path": clean}
    return {
        "parsed": parsed,
        "files": files,
        "skipped": left_out + skipped,
        "source": {
            "type": "github",
            "key": f"github:{pinned['owner'].lower()}/{pinned['repo'].lower()}:{clean}",
            "url": f"https://github.com/{pinned['owner']}/{pinned['repo']}/tree/{pinned['sha']}/{clean}".rstrip("/"),
            "repo_url": f"https://github.com/{pinned['owner']}/{pinned['repo']}",
            "owner": pinned["owner"],
            "repo": pinned["repo"],
            "path": clean,
            "ref": pinned["ref"],
            "sha": pinned["sha"],
            "label": _label(here),
        },
    }


async def latest_sha(source: dict) -> str:
    """The commit the imported skill's branch points at now (for "check for updates")."""
    async with _client() as client:
        pinned = await resolve({"owner": source["owner"], "repo": source["repo"], "ref": source.get("ref") or "", "path": ""}, client)
    return pinned["sha"]
