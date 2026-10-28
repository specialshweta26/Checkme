"""Harness tools for computer, web, memory, skills, scratchpad, and task_catalog."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agent import computer_harness as computer
from app.agent import scratchpad as pad
from app.agent import task_catalog as tasks
from app.agent import web_tools
from app.agent.memory import forget_memory, read_history, recall_memory, remember_fact, search_history
from app.skills import service as skills


def _dumps(value: Any, limit: int = 60000) -> str:
    from app.agent.deep import dumps

    return dumps(value, limit)


class UrlInput(BaseModel):
    url: str = Field(description="http or https URL.")
    wait_ms: int = Field(default=0, description="Extra wait after load in ms. 0 = default.")
    tab: str = Field(default="", description="Optional tab id (t1, t2 from browser_tabs); works on that tab without switching the screen. Default: active tab.")


class BrowserActInput(BaseModel):
    actions: list[dict[str, Any]] = Field(
        description="Ordered steps, e.g. [{kind: click, ref: e3}]. kind: click, fill, type, select, check, press, scroll, wait, reload, back."
    )
    tab: str = Field(default="", description="Optional tab id (t1, t2 from browser_tabs); works on that tab without switching the screen. Default: active tab.")


class TabInput(BaseModel):
    tab: str = Field(default="", description="Optional tab id (t1, t2 from browser_tabs); works on that tab without switching the screen. Default: active tab.")


class NewTabInput(BaseModel):
    url: str = Field(default="", description="Optional http(s) URL to open in the new tab.")
    background: bool = Field(default=False, description="Open without switching the live screen to it.")


class ComputerActInput(BaseModel):
    actions: list[dict[str, Any]] = Field(
        description="Desktop actions: {kind, x, y, text, key, direction, amount, ms}."
    )


class ShellInput(BaseModel):
    command: str = Field(description="Bash command. Chain steps with &&.")
    cwd: str | None = Field(default=None, description="Working directory inside the computer.")
    timeout: int = Field(default=60, description="Seconds to wait (10-900).")


class PathInput(BaseModel):
    path: str = Field(default="", description="Absolute or workspace-relative path.")


class WriteFileInput(BaseModel):
    path: str
    content: str


class AttachFileInput(BaseModel):
    path: str = Field(description="Sandbox path to attach.")
    filename: str = Field(default="", description="Download name shown to the user.")


class LaunchAppInput(BaseModel):
    name: str = Field(description="App to open on the computer: browser, chrome, code, or a short binary name.")


class TakeoverInput(BaseModel):
    reason: str = Field(description="Why the user must take over (login, 2FA, captcha, payment).")
    instruction: str = Field(default="", description="What they should do on the live screen.")


class WebSearchInput(BaseModel):
    query: str = Field(description="Search query. Use the user's words, not a capability phrase.")
    count: int = Field(default=8, description="How many results (1-12).")


class ResearchInput(BaseModel):
    goal: str = Field(default="", description="What you are trying to find out; links are scored against it.")
    seeds: list[str] = Field(default_factory=list, description="Start URLs.")
    queries: list[str] = Field(default_factory=list, description="Web searches whose results become start URLs (up to 5).")
    max_pages: int = Field(default=30, description="Page budget (max 300).")
    max_depth: int = Field(default=2, description="How many links deep to follow from a start URL (max 5).")
    same_site: bool = Field(default=False, description="Only follow links on the start sites.")
    crawl_id: str = Field(default="", description="Resume an earlier crawl instead of starting one.")
    more_pages: int = Field(default=0, description="When resuming: extra pages to read.")


class RankLinksInput(BaseModel):
    goal: str = Field(description="What you are looking for on this site.")
    tab: str = Field(default="", description="Optional tab id; default the active tab.")
    top: int = Field(default=12, description="How many links.")


class ResearchNotesInput(BaseModel):
    crawl_id: str
    query: str = Field(default="", description="Rank notes by this narrower question.")
    top: int = Field(default=15, description="How many notes (max 60).")


class FetchInput(BaseModel):
    url: str = Field(description="http or https URL to fetch.")


class PlotInput(BaseModel):
    title: str
    labels: list[str] = Field(description="Category or time labels.")
    values: list[float] = Field(description="Numeric values, same length as labels.")
    chart_type: str = Field(default="bar", description="line, bar, or pie.")
    series_name: str = Field(default="")


class ImageInput(BaseModel):
    prompt: str = Field(description="What to draw.")
    size: str = Field(default="1024x1024", description="1024x1024, 1024x1792, or 1792x1024.")


class VideoInput(BaseModel):
    prompt: str = Field(default="", description="What to generate.")


class RememberInput(BaseModel):
    text: str = Field(description="The fact or preference to keep.")
    label: str = Field(default="", description="Optional core label: prefs, people, voice, work, coding.")
    shared: bool = Field(default=False, description="True to save on Chief for every agent to read.")


class RecallInput(BaseModel):
    query: str = Field(default="", description="What to look up. Empty returns recent memory.")
    label: str = Field(default="", description="Optional core label to filter.")


class ForgetInput(BaseModel):
    memory_id: str = Field(default="", description="Passage id from recall_memory.")
    label: str = Field(default="", description="Core block label to clear or edit.")
    text: str = Field(default="", description="Snippet to match if you do not have an id.")


class HistorySearchInput(BaseModel):
    query: str = Field(description="Words to find in this chat.")
    limit: int = Field(default=12)


class HistoryReadInput(BaseModel):
    limit: int = Field(default=8, description="How many recent messages in this chat.")


class SkillReadInput(BaseModel):
    skill_id: str = Field(default="", description="Skill id if you have it.")
    name: str = Field(default="", description="Skill name if you do not have an id.")


class SkillCreateInput(BaseModel):
    name: str = Field(description="Short name for this workflow.")
    instruction: str = Field(default="", description="What to do when this skill runs.")


class SkillUpdateInput(BaseModel):
    skill_id: str = Field(default="")
    name: str = Field(default="", description="Current name, if no id.")
    new_name: str = Field(default="", description="Rename.")
    instruction: str = Field(default="", description="Replacement instruction. Empty leaves it unchanged.")


class SkillDeleteInput(BaseModel):
    skill_id: str = Field(default="")
    name: str = Field(default="")


class ScratchAddInput(BaseModel):
    text: str = Field(description="Checklist item for this chat.")


class ScratchItemInput(BaseModel):
    item_id: str = Field(description="Scratchpad id such as s1.")
    text: str = Field(default="", description="Replacement text.")
    status: str = Field(default="", description="open or done.")


class ScratchIdInput(BaseModel):
    item_id: str


class TaskCatalogInput(BaseModel):
    action: str = Field(description="search, load, or execute.")
    query: str = Field(default="", description="For search: what kind of task.")
    name: str = Field(default="", description="For load/execute: task name from search.")
    args: dict[str, Any] = Field(default_factory=dict, description="For execute: arguments from load.")


def extra_harness_tools(ctx) -> list[StructuredTool]:
    async def computer_observe() -> str:
        """Screenshot the live computer screen. Use when page tools fail or you need coordinates for computer_act."""
        ctx.used_tools.append("computer_observe")
        return _dumps(await computer.run_computer(ctx, "computer_observe", {}))

    async def computer_act(actions: list[dict[str, Any]]) -> str:
        """Desktop actions on the live computer screen: click/move at x,y, type, key, scroll, wait. Fallback when browser_act cannot use refs."""
        ctx.used_tools.append("computer_act")
        return _dumps(await computer.run_computer(ctx, "computer_act", {"actions": actions}))

    async def browser_navigate(url: str, wait_ms: int = 0, tab: str = "") -> str:
        """Open an http(s) URL in the live computer browser (the same screen the chat shows) and return its snapshot. Prefer the destination site over a Google Search URL. If the page is a CAPTCHA or verification wall, request_takeover — do not invent results."""
        ctx.used_tools.append("browser_navigate")
        args: dict[str, Any] = {"url": url}
        if wait_ms:
            args["wait_ms"] = wait_ms
        if tab:
            args["tab"] = tab
        return _dumps(await computer.run_computer(ctx, "browser_navigate", args))

    async def browser_snapshot(tab: str = "") -> str:
        """Re-read the live computer browser: URL, title, page text, and interactive refs (e0, e1). browser_navigate and browser_act already return one."""
        ctx.used_tools.append("browser_snapshot")
        return _dumps(await computer.run_computer(ctx, "browser_snapshot", {"tab": tab} if tab else {}))

    async def browser_act(actions: list[dict[str, Any]], tab: str = "") -> str:
        """Act on the live computer browser using snapshot refs (e0, e1). kind: click, fill, type, select, check, press, scroll, wait, reload, back. Returns a fresh snapshot."""
        ctx.used_tools.append("browser_act")
        args: dict[str, Any] = {"actions": actions}
        if tab:
            args["tab"] = tab
        return _dumps(await computer.run_computer(ctx, "browser_act", args))

    async def browser_tabs() -> str:
        """List open browser tabs (t1, t2): URL, title, and which is active on the live screen."""
        ctx.used_tools.append("browser_tabs")
        return _dumps(await computer.run_computer(ctx, "browser_tabs", {}))

    async def browser_new_tab(url: str = "", background: bool = False) -> str:
        """Open a new tab (optionally at a URL) and return its tab id and snapshot. Keep several pages open, e.g. the same search on different sites, and work each by passing tab= to browser_navigate / browser_snapshot / browser_act."""
        ctx.used_tools.append("browser_new_tab")
        return _dumps(await computer.run_computer(ctx, "browser_new_tab", {"url": url, "background": background}))

    async def browser_switch_tab(tab: str = "") -> str:
        """Show a tab on the live screen (make it active) and return its snapshot."""
        ctx.used_tools.append("browser_switch_tab")
        return _dumps(await computer.run_computer(ctx, "browser_switch_tab", {"tab": tab}))

    async def browser_close_tab(tab: str = "") -> str:
        """Close a tab (default: the active one)."""
        ctx.used_tools.append("browser_close_tab")
        return _dumps(await computer.run_computer(ctx, "browser_close_tab", {"tab": tab}))

    async def shell(command: str, cwd: str | None = None, timeout: int = 60) -> str:
        """Run a bash command on this user's cloud computer (sandbox). Not the user's real laptop — use user_computer_shell for that."""
        ctx.used_tools.append("shell")
        return _dumps(await computer.shell(ctx, command, cwd, timeout), 40000)

    async def user_computer_shell(command: str, cwd: str | None = None, timeout: int = 60) -> str:
        """Run a bash command on the user's trusted local computer. Never use this for the cloud sandbox. Requires a local computer connection and confirmation."""
        ctx.used_tools.append("user_computer_shell")
        return _dumps(await computer.user_computer_shell(ctx, command, cwd, timeout), 40000)

    async def list_files(path: str = "") -> str:
        """List files and folders on the computer. Default is the workspace."""
        ctx.used_tools.append("list_files")
        return _dumps(await computer.list_files(ctx, path))

    async def read_file(path: str) -> str:
        """Read a UTF-8 file from the computer."""
        ctx.used_tools.append("read_file")
        return _dumps(await computer.read_file(ctx, path), 32000)

    async def write_file(path: str, content: str) -> str:
        """Write a UTF-8 file on the computer, creating parent directories."""
        ctx.used_tools.append("write_file")
        return _dumps(await computer.write_file(ctx, path, content))

    async def attach_file(path: str, filename: str = "") -> str:
        """Copy a computer file into a download link in this chat. Use after write_file or run_code. Cite download_url, never a sandbox path."""
        ctx.used_tools.append("attach_file")
        return _dumps(await computer.attach_file(ctx, path, filename))

    async def open_path(path: str) -> str:
        """Open a path on the computer: list a directory, read a text file, or attach a binary so the user can download it."""
        ctx.used_tools.append("open_path")
        return _dumps(await computer.open_path(ctx, path), 32000)

    async def launch_app(name: str) -> str:
        """Open a known app on the cloud computer (browser, chrome, code). The live screen is already the browser."""
        ctx.used_tools.append("launch_app")
        return _dumps(await computer.launch_app(ctx, name))

    async def request_takeover(reason: str, instruction: str = "") -> str:
        """Pause and give the user control of the live computer screen. Use for login, 2FA, captcha, payment, or any step you cannot complete. Same as browser_handoff."""
        ctx.used_tools.append("request_takeover")
        return _dumps(await computer.request_takeover(ctx, reason, instruction))

    async def web_search(query: str, count: int = 8) -> str:
        """Search the public web. Use for facts, news, and 'what is going on'. Then web_fetch or browser_navigate the best URLs. Do not search_tools for this. For live bookings, inventory, or forms, browser_navigate the site itself instead of a Google Search URL. If it returns an error or no results, say so and never make results up."""
        ctx.used_tools.append("web_search")
        return _dumps(await web_tools.web_search(query, count))

    async def web_fetch(url: str) -> str:
        """Fetch a public http(s) page. Paints it onto the computer screen when the sandbox is available; otherwise returns page text."""
        ctx.used_tools.append("web_fetch")
        return _dumps(await web_tools.web_fetch(ctx, url), 32000)

    async def research_crawl(
        goal: str = "",
        seeds: list[str] | None = None,
        queries: list[str] | None = None,
        max_pages: int = 30,
        max_depth: int = 2,
        same_site: bool = False,
        crawl_id: str = "",
        more_pages: int = 0,
    ) -> str:
        """Read many pages toward a goal without filling your context: fetches start URLs (or search results) in parallel over HTTP, follows the links most relevant to the goal (best-first, deduped, page/depth budgets), and returns short per-page notes ranked by relevance plus pages that need the real browser. Checkpointed: resume with crawl_id and more_pages. Use for research across many sources; for one page use web_fetch."""
        from app.agent import research

        ctx.used_tools.append("research_crawl")
        return _dumps(
            await research.run_research_tool(
                ctx, goal, seeds, queries, max_pages, max_depth, same_site, crawl_id, more_pages
            ),
            40000,
        )

    async def browser_rank_links(goal: str, tab: str = "", top: int = 12) -> str:
        """Rank the links on the current browser page (or tab) by relevance to a goal; returns refs and URLs to click or open in new tabs. Use to go deeper on a site without guessing."""
        from app.agent import research

        ctx.used_tools.append("browser_rank_links")
        snap = await computer.run_computer(ctx, "browser_snapshot", {"tab": tab} if tab else {})
        if not isinstance(snap, dict) or snap.get("ok") is False:
            return _dumps(snap)
        return _dumps(research.rank_snapshot_links(goal, snap, top))

    async def research_notes(crawl_id: str, query: str = "", top: int = 15) -> str:
        """Read more notes from a research_crawl, optionally ranked by a narrower question (map step for synthesis)."""
        from app.agent import research

        ctx.used_tools.append("research_notes")
        return _dumps(await research.research_notes_tool(ctx, crawl_id, query, top), 40000)

    async def render_plot(title: str, labels: list[str], values: list[float], chart_type: str = "bar", series_name: str = "") -> str:
        """Build a downloadable chart (line/bar/pie) on the computer and return download_url. Pass labels and numeric values."""
        ctx.used_tools.append("render_plot")
        return _dumps(await computer.render_plot(ctx, title, labels, values, chart_type, series_name))

    async def generate_image(prompt: str, size: str = "1024x1024") -> str:
        """Generate an image from a prompt. Returns a URL. Do not use this for charts of real data — call render_plot."""
        ctx.used_tools.append("generate_image")
        return _dumps(await web_tools.generate_image(prompt, size))

    async def generate_video(prompt: str = "") -> str:
        """Generate a video. Returns not-configured until a video provider is connected."""
        ctx.used_tools.append("generate_video")
        return _dumps(web_tools.generate_video(prompt))

    async def remember(text: str, label: str = "", shared: bool = False) -> str:
        """Save a lasting fact or preference. Use label prefs|people|voice|work|coding for standing notes. Set shared=true so every agent can read it. Only call when the user asked to remember, or the fact is clearly a preference."""
        ctx.used_tools.append("remember")
        return _dumps(await remember_fact(ctx, text, label, shared))

    async def save_memory(text: str, label: str = "", shared: bool = False) -> str:
        """Same as remember: save a fact on this agent (or shared=true for every agent)."""
        ctx.used_tools.append("save_memory")
        return _dumps(await remember_fact(ctx, text, label, shared))

    async def save_shared_memory(text: str, label: str = "", shared: bool = True) -> str:
        """Save a fact on Chief so every agent can read it."""
        ctx.used_tools.append("save_shared_memory")
        return _dumps(await remember_fact(ctx, text, label, True))

    async def recall(query: str = "", label: str = "") -> str:
        """Look up saved memory for this agent. Pass query or a label (prefs, people, voice, work, coding)."""
        ctx.used_tools.append("recall_memory")
        return _dumps(await recall_memory(ctx, query, label))

    async def forget(memory_id: str = "", label: str = "", text: str = "") -> str:
        """Delete a saved memory. Pass memory_id from recall_memory, a core label, or a snippet of the text."""
        ctx.used_tools.append("forget_memory")
        return _dumps(await forget_memory(ctx, memory_id, label, text))

    async def search_hist(query: str, limit: int = 12) -> str:
        """Search this chat's messages for a phrase."""
        ctx.used_tools.append("search_history")
        return _dumps(await search_history(ctx, query, limit))

    async def read_hist(limit: int = 8) -> str:
        """Read the most recent messages in this chat."""
        ctx.used_tools.append("read_history")
        return _dumps(await read_history(ctx, limit))

    def _agent_id():
        return getattr(ctx.agent, "id", None)

    async def skill_read(skill_id: str = "", name: str = "") -> str:
        """Read a saved skill (workflow) by id or name."""
        ctx.used_tools.append("skill_read")
        if ctx.db is None:
            return _dumps({"error": "Cannot read skills without a workspace."})
        row = await skills.get_skill(ctx.db, ctx.user, skill_id, name, _agent_id())
        if row is None:
            listed = await skills.list_skills(ctx.db, ctx.user, _agent_id())
            return _dumps({"error": "Unknown skill.", "skills": listed})
        return _dumps(skills.serialize_skill(row))

    async def skill_create(name: str, instruction: str = "") -> str:
        """Save a workflow this bot should reuse later."""
        ctx.used_tools.append("skill_create")
        if ctx.db is None:
            return _dumps({"error": "Cannot save a skill without a workspace."})
        try:
            row = await skills.save_skill(
                ctx.db, ctx.user, name=name, agent_id=_agent_id(), instruction=instruction, kind="saved"
            )
        except skills.SkillError as exc:
            return _dumps({"error": str(exc)})
        return _dumps({"ok": True, "id": str(row.id), "name": row.name})

    async def skill_update(skill_id: str = "", name: str = "", new_name: str = "", instruction: str = "") -> str:
        """Rename a skill or replace its instruction."""
        ctx.used_tools.append("skill_update")
        if ctx.db is None:
            return _dumps({"error": "Cannot update a skill without a workspace."})
        try:
            row = await skills.update_skill(
                ctx.db,
                ctx.user,
                skill_id=skill_id,
                name=name,
                new_name=new_name,
                instruction=instruction if instruction else None,
                agent_id=_agent_id(),
            )
        except skills.SkillError as vis_exc:
            return _dumps({"error": str(vis_exc)})
        return _dumps(skills.serialize_skill(row))

    async def skill_delete(skill_id: str = "", name: str = "") -> str:
        """Delete a saved skill."""
        ctx.used_tools.append("skill_delete")
        if ctx.db is None:
            return _dumps({"error": "Cannot delete a skill without a workspace."})
        try:
            return _dumps(await skills.delete_skill(ctx.db, ctx.user, skill_id, name, _agent_id()))
        except skills.SkillError as vis_exc:
            return _dumps({"error": str(vis_exc)})

    async def scratchpad_list() -> str:
        """List this chat's working checklist."""
        ctx.used_tools.append("scratchpad_list")
        return _dumps(await pad.listing(ctx))

    async def scratchpad_add(text: str) -> str:
        """Add an item to this chat's working checklist. Use for multi-step work in this turn."""
        ctx.used_tools.append("scratchpad_add")
        return _dumps(await pad.add(ctx, text))

    async def scratchpad_update(item_id: str, text: str = "", status: str = "") -> str:
        """Update a scratchpad item's text or status (open/done)."""
        ctx.used_tools.append("scratchpad_update")
        return _dumps(await pad.update(ctx, item_id, text, status))

    async def scratchpad_complete(item_id: str) -> str:
        """Mark a scratchpad item done."""
        ctx.used_tools.append("scratchpad_complete")
        return _dumps(await pad.complete(ctx, item_id))

    async def scratchpad_remove(item_id: str) -> str:
        """Remove a scratchpad item."""
        ctx.used_tools.append("scratchpad_remove")
        return _dumps(await pad.remove(ctx, item_id))

    async def task_catalog(action: str, query: str = "", name: str = "", args: dict[str, Any] | None = None) -> str:
        """Find or run a built-in task (schedule, briefing, inbox, coding job, generate image). action=search|load|execute. Search first, then load, then execute. Do not dump this catalog into call_tool."""
        ctx.used_tools.append("task_catalog")
        verb = (action or "search").strip().lower()
        if verb == "search":
            return _dumps({"tasks": tasks.search(query)})
        if verb == "load":
            return _dumps(tasks.load(name))
        if verb == "execute":
            return _dumps(await tasks.execute(ctx, name, args or {}))
        return _dumps({"error": "action must be search, load, or execute", "tasks": tasks.search("")})

    return [
        StructuredTool.from_function(coroutine=computer_observe, name="computer_observe", description=computer_observe.__doc__),
        StructuredTool.from_function(coroutine=computer_act, name="computer_act", description=computer_act.__doc__, args_schema=ComputerActInput),
        StructuredTool.from_function(coroutine=browser_navigate, name="browser_navigate", description=browser_navigate.__doc__, args_schema=UrlInput),
        StructuredTool.from_function(coroutine=browser_snapshot, name="browser_snapshot", description=browser_snapshot.__doc__, args_schema=TabInput),
        StructuredTool.from_function(coroutine=browser_act, name="browser_act", description=browser_act.__doc__, args_schema=BrowserActInput),
        StructuredTool.from_function(coroutine=browser_tabs, name="browser_tabs", description=browser_tabs.__doc__),
        StructuredTool.from_function(coroutine=browser_new_tab, name="browser_new_tab", description=browser_new_tab.__doc__, args_schema=NewTabInput),
        StructuredTool.from_function(coroutine=browser_switch_tab, name="browser_switch_tab", description=browser_switch_tab.__doc__, args_schema=TabInput),
        StructuredTool.from_function(coroutine=browser_close_tab, name="browser_close_tab", description=browser_close_tab.__doc__, args_schema=TabInput),
        StructuredTool.from_function(coroutine=shell, name="shell", description=shell.__doc__, args_schema=ShellInput),
        StructuredTool.from_function(coroutine=user_computer_shell, name="user_computer_shell", description=user_computer_shell.__doc__, args_schema=ShellInput),
        StructuredTool.from_function(coroutine=list_files, name="list_files", description=list_files.__doc__, args_schema=PathInput),
        StructuredTool.from_function(coroutine=read_file, name="read_file", description=read_file.__doc__, args_schema=PathInput),
        StructuredTool.from_function(coroutine=write_file, name="write_file", description=write_file.__doc__, args_schema=WriteFileInput),
        StructuredTool.from_function(coroutine=attach_file, name="attach_file", description=attach_file.__doc__, args_schema=AttachFileInput),
        StructuredTool.from_function(coroutine=open_path, name="open_path", description=open_path.__doc__, args_schema=PathInput),
        StructuredTool.from_function(coroutine=launch_app, name="launch_app", description=launch_app.__doc__, args_schema=LaunchAppInput),
        StructuredTool.from_function(coroutine=request_takeover, name="request_takeover", description=request_takeover.__doc__, args_schema=TakeoverInput),
        StructuredTool.from_function(coroutine=web_search, name="web_search", description=web_search.__doc__, args_schema=WebSearchInput),
        StructuredTool.from_function(coroutine=web_fetch, name="web_fetch", description=web_fetch.__doc__, args_schema=FetchInput),
        StructuredTool.from_function(coroutine=research_crawl, name="research_crawl", description=research_crawl.__doc__, args_schema=ResearchInput),
        StructuredTool.from_function(coroutine=browser_rank_links, name="browser_rank_links", description=browser_rank_links.__doc__, args_schema=RankLinksInput),
        StructuredTool.from_function(coroutine=research_notes, name="research_notes", description=research_notes.__doc__, args_schema=ResearchNotesInput),
        StructuredTool.from_function(coroutine=render_plot, name="render_plot", description=render_plot.__doc__, args_schema=PlotInput),
        StructuredTool.from_function(coroutine=generate_image, name="generate_image", description=generate_image.__doc__, args_schema=ImageInput),
        StructuredTool.from_function(coroutine=generate_video, name="generate_video", description=generate_video.__doc__, args_schema=VideoInput),
        StructuredTool.from_function(coroutine=remember, name="remember", description=remember.__doc__, args_schema=RememberInput),
        StructuredTool.from_function(coroutine=save_memory, name="save_memory", description=save_memory.__doc__, args_schema=RememberInput),
        StructuredTool.from_function(coroutine=save_shared_memory, name="save_shared_memory", description=save_shared_memory.__doc__, args_schema=RememberInput),
        StructuredTool.from_function(coroutine=recall, name="recall_memory", description=recall.__doc__, args_schema=RecallInput),
        StructuredTool.from_function(coroutine=forget, name="forget_memory", description=forget.__doc__, args_schema=ForgetInput),
        StructuredTool.from_function(coroutine=search_hist, name="search_history", description=search_hist.__doc__, args_schema=HistorySearchInput),
        StructuredTool.from_function(coroutine=read_hist, name="read_history", description=read_hist.__doc__, args_schema=HistoryReadInput),
        StructuredTool.from_function(coroutine=skill_read, name="skill_read", description=skill_read.__doc__, args_schema=SkillReadInput),
        StructuredTool.from_function(coroutine=skill_create, name="skill_create", description=skill_create.__doc__, args_schema=SkillCreateInput),
        StructuredTool.from_function(coroutine=skill_update, name="skill_update", description=skill_update.__doc__, args_schema=SkillUpdateInput),
        StructuredTool.from_function(coroutine=skill_delete, name="skill_delete", description=skill_delete.__doc__, args_schema=SkillDeleteInput),
        StructuredTool.from_function(coroutine=scratchpad_list, name="scratchpad_list", description=scratchpad_list.__doc__),
        StructuredTool.from_function(coroutine=scratchpad_add, name="scratchpad_add", description=scratchpad_add.__doc__, args_schema=ScratchAddInput),
        StructuredTool.from_function(coroutine=scratchpad_update, name="scratchpad_update", description=scratchpad_update.__doc__, args_schema=ScratchItemInput),
        StructuredTool.from_function(coroutine=scratchpad_complete, name="scratchpad_complete", description=scratchpad_complete.__doc__, args_schema=ScratchIdInput),
        StructuredTool.from_function(coroutine=scratchpad_remove, name="scratchpad_remove", description=scratchpad_remove.__doc__, args_schema=ScratchIdInput),
        StructuredTool.from_function(coroutine=task_catalog, name="task_catalog", description=task_catalog.__doc__, args_schema=TaskCatalogInput),
    ]
