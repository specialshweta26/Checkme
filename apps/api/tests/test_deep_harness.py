import json
from pathlib import Path
from types import SimpleNamespace

from app.agent.deep import (
    RunContext,
    _computer_session,
    _connect_cards,
    _is_connect_query,
    _maybe_start_connect,
    _pick_connect_toolkit,
    call_user_tool,
    compact_payload,
    dumps,
    dumps_full,
    harness_tools,
    inspect_user_result,
    last_text,
    list_user_connectors,
    search_user_tools,
    system_prompt,
)


def _conn(**kwargs):
    defaults = {
        "id": "1",
        "name": "mcp-gateway",
        "alias": "digital",
        "slug": "digital",
        "kind": "mcp",
        "provider": "mcp",
        "description": "Retail KPI gateway",
        "server_key": "mcp:digital",
        "health": "ok",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _ctx(connectors, **kwargs):
    user = SimpleNamespace(id="u1", timezone="Asia/Kolkata")
    return RunContext(db=None, user=user, connectors={c.slug: c for c in connectors}, **kwargs)


async def test_list_connectors_is_this_user_only():
    mine = _conn()
    ctx = _ctx([mine])
    listed = await list_user_connectors(ctx)
    assert [item["slug"] for item in listed] == ["digital"]
    assert "gmail" not in json.dumps(listed)


def test_catalog_dumps_are_not_truncated():
    listed = [
        {"slug": "gmail", "core_tools": [f"mail_tool_{i}" for i in range(80)]},
        {"slug": "outlook", "core_tools": [f"outlook_tool_{i}" for i in range(80)]},
        {"slug": "digital", "core_tools": [f"kpi_{i}" for i in range(80)], "kind": "mcp"},
    ]
    raw = dumps_full(listed)
    data = json.loads(raw)
    assert isinstance(data, list)
    assert [row["slug"] for row in data] == ["gmail", "outlook", "digital"]
    assert "too_large" not in raw
    assert len(data[2]["core_tools"]) == 80
    compact = json.loads(dumps(listed, 2000))
    assert isinstance(compact, dict) and compact.get("too_large") is True


async def test_browser_handoff_pauses_for_user():
    computer = _conn(slug="computer", provider="daytona", kind="builtin", name="Computer", alias="computer")
    ctx = _ctx([computer])
    result = await call_user_tool(ctx, "computer", "browser_handoff", {"reason": "Log in to the site."})
    assert result["handoff"] is True
    assert ctx.pending[0]["kind"] == "handoff"
    ctx.confirmations = [{"slug": "computer", "tool": "browser_handoff"}]
    done = await call_user_tool(ctx, "computer", "browser_handoff", {"reason": "Log in to the site."})
    assert done["status"] == "user_finished"
    assert done["handoff"] is False


async def test_unknown_slug_is_rejected():
    ctx = _ctx([_conn()])
    result = await call_user_tool(ctx, "gmail", "list_messages", {})
    assert result["error"]
    assert "gmail" in result["error"]
    assert ctx.used_tools == []


async def test_multi_call_records_each_execute_source():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append((slug, tool, args))
        window = (args.get("arguments") or args).get("start") or ""
        return {"kpis": {"net_sales": {"value": 10 if window.startswith("2026-07") else 20}}}

    ctx = _ctx([_conn()], call_impl=fake_call)
    first = await call_user_tool(
        ctx,
        "digital",
        "gateway__execute",
        {"tool_name": "get_sales_dashboard", "arguments": {"brand": "acme", "start": "2026-07-01", "end": "2026-07-31"}},
    )
    second = await call_user_tool(
        ctx,
        "digital",
        "gateway__execute",
        {"tool_name": "get_sales_dashboard", "arguments": {"brand": "acme", "start": "2026-08-01", "end": "2026-08-31"}},
    )
    assert first["kpis"]["net_sales"]["value"] == 10
    assert second["kpis"]["net_sales"]["value"] == 20
    assert len(calls) == 2
    assert ctx.used_tools.count("digital__gateway__execute") == 2
    assert ctx.sources[-1]["source_tool"] == "digital__get_sales_dashboard"
    assert "get_sales_dashboard" in json.dumps(ctx.sources)


def _gateway_surface(**kwargs):
    surface = {
        "search": "gateway__tool_search",
        "search_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "execute": "gateway__execute",
        "resolve": None,
        "resolve_schema": {},
    }
    surface.update(kwargs)
    return surface


def test_mail_scope_ignores_chat_apps():
    from app.routing.retrieve import extra_capability_terms, is_calendar_query, keep_connector_scope

    chat = "search Teams messages by sender"
    assert "teams" in extra_capability_terms(chat)
    assert not is_calendar_query(chat)
    assert keep_connector_scope("outlook", chat) is False
    assert keep_connector_scope("outlook", "search messages by sender") is True
    assert keep_connector_scope("outlook", "teams meeting with shyam") is True
    assert keep_connector_scope("composio", chat) is True


def test_pick_connect_toolkit_uses_named_app():
    apps = [
        {"slug": "replyio", "name": "Reply.io"},
        {"slug": "gmail", "name": "Gmail"},
        {"slug": "github", "name": "GitHub"},
    ]
    picked = _pick_connect_toolkit(apps, "connect to gmail")
    assert picked["slug"] == "gmail"


async def test_connect_query_starts_composio_card():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append((tool, dict(args)))
        if tool == "list_apps":
            return {"apps": [{"slug": "gmail", "name": "Gmail", "logo": "https://example.com/g.png"}]}
        if tool == "list_connections":
            return {"connections": []}
        if tool == "connect_app":
            return {
                "toolkit": args.get("toolkit"),
                "name": "Gmail",
                "redirect_url": "https://connect.composio.dev/link/gmail",
            }
        return {}

    market = _conn(slug="composio", provider="composio", kind="builtin", name="Composio", alias="composio")
    ctx = _ctx([market], call_impl=fake_call)
    ctx.gateway_surface["composio"] = {
        "search": "find_tools",
        "search_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "execute": "execute",
        "resolve": "resolve",
        "resolve_schema": {},
    }
    await _maybe_start_connect(ctx, "connect to gmail")
    cards = _connect_cards(ctx)
    assert any(tool == "connect_app" and args.get("toolkit") == "gmail" for tool, args in calls)
    assert cards[0]["redirect_url"] == "https://connect.composio.dev/link/gmail"
    assert not any(tool == "execute" for tool, _ in calls)


def test_connected_is_not_a_connect_request():
    assert _is_connect_query("connect to figma")
    assert _is_connect_query("please authorize slack")
    assert not _is_connect_query("just check it is connected")
    assert not _is_connect_query("it is connected now")
    assert not _is_connect_query("Figma is connected")


async def test_already_connected_app_does_not_open_another_oauth_card():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append((tool, dict(args)))
        if tool == "list_apps":
            return {"apps": [{"slug": "figma", "name": "Figma"}]}
        if tool == "list_connections":
            return {"connections": [{"toolkit": "figma", "status": "ACTIVE"}]}
        if tool == "connect_app":
            return {"toolkit": "figma", "redirect_url": "https://connect.composio.dev/link/figma"}
        return {}

    market = _conn(slug="composio", provider="composio", kind="builtin", name="Composio", alias="composio")
    ctx = _ctx([market], call_impl=fake_call)
    await _maybe_start_connect(ctx, "connect to figma")
    assert any(tool == "list_connections" for tool, _ in calls)
    assert not any(tool == "connect_app" for tool, _ in calls)
    assert _connect_cards(ctx) == []


async def test_list_connectors_includes_connected_marketplace_apps():
    async def fake_call(slug, tool, args):
        if tool == "list_connections":
            return {"connections": [{"toolkit": "figma", "status": "ACTIVE"}]}
        return {}

    market = _conn(slug="composio", provider="composio", kind="builtin", name="Composio", alias="composio")
    computer = _conn(slug="computer", provider="daytona", kind="builtin", name="Computer", alias="computer")
    ctx = _ctx([computer, market], call_impl=fake_call)
    listed = await list_user_connectors(ctx)
    slugs = [item["slug"] for item in listed]
    assert "computer" in slugs
    assert "composio" in slugs
    assert "figma" in slugs


def test_computer_session_marks_live_browser():
    ctx = _ctx([_conn()])
    ctx.used_tools.append("computer__browser_navigate")
    ctx.results.append(
        {
            "tool_name": "browser_navigate",
            "data": {
                "url": "https://example.com",
                "title": "Example",
                "screenshot": {"preview_url": "/api/files/page.png", "download_url": "/api/files/page.png"},
            },
        }
    )
    session = _computer_session(ctx)
    assert session["live"] is True
    assert session["url"] == "https://example.com"
    assert session["preview_url"] == "/api/files/page.png"


def test_computer_session_uses_screen_attachment():
    ctx = _ctx([_conn()])
    ctx.attachments.append({"kind": "screen", "filename": "page.png", "preview_url": "/api/files/x.png", "download_url": "/api/files/x.png"})
    session = _computer_session(ctx)
    assert session["live"] is True
    assert session["preview_url"] == "/api/files/x.png"


def test_connect_cards_come_from_redirect_url():
    ctx = _ctx([_conn()], agent=SimpleNamespace(id="aa", name="tony"))
    ctx.results.append(
        {
            "tool_name": "connect_app",
            "data": {
                "toolkit": "gmail",
                "name": "Gmail",
                "redirect_url": "https://connect.composio.dev/link/abc",
            },
        }
    )
    cards = _connect_cards(ctx)
    assert cards[0]["toolkit"] == "gmail"
    assert cards[0]["redirect_url"].startswith("https://connect.composio.dev")
    assert cards[0]["agent_id"] == "aa"
    assert cards[0]["agent_name"] == "tony"


def test_connect_cards_include_api_key_apps():
    ctx = _ctx([_conn()], agent=SimpleNamespace(id="aa", name="tony"))
    ctx.results.append(
        {
            "tool_name": "connect_app",
            "data": {
                "toolkit": "serpapi",
                "name": "SerpApi",
                "needs_credentials": True,
                "fields": [{"name": "generic_api_key", "label": "API Key"}],
            },
        }
    )
    cards = _connect_cards(ctx)
    assert cards[0]["toolkit"] == "serpapi"
    assert cards[0]["needs_credentials"] is True


async def test_connect_query_does_not_expand_marketplace_execute_tools():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append(tool)
        return {"candidates": [{"public_name": "REPLYIO_SEND_GMAIL", "description": "Send via Reply.io"}]}

    market = _conn(slug="composio", provider="composio", kind="builtin", name="Composio", alias="composio")
    ctx = _ctx([market], call_impl=fake_call)
    ctx.gateway_surface["composio"] = {
        "search": "find_tools",
        "search_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "execute": "execute",
        "resolve": "resolve",
        "resolve_schema": {},
    }
    rows = await search_user_tools(ctx, "connect to gmail")
    assert "find_tools" not in calls
    assert not any(row["tool_name"] == "REPLYIO_SEND_GMAIL" for row in rows)


async def test_outlook_slug_still_expands_marketplace_for_chat():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append((slug, tool, dict(args)))
        if tool == "list_connections":
            return {"connections": [{"toolkit": "microsoft_teams", "status": "ACTIVE"}]}
        if tool == "find_tools":
            return {
                "candidates": [
                    {"public_name": "TEAMS_LIST_CHATS", "description": "List Microsoft Teams chats", "score": 0.9}
                ]
            }
        return {"candidates": []}

    outlook = _conn(slug="outlook", provider="outlook", kind="builtin", name="Outlook", alias="outlook")
    market = _conn(slug="composio", provider="composio", kind="builtin", name="Composio", alias="composio")
    ctx = _ctx([outlook, market], call_impl=fake_call)
    ctx.gateway_surface["composio"] = {
        "search": "find_tools",
        "search_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "toolkit": {"type": "string"}},
        },
        "execute": "execute",
        "resolve": "resolve",
        "resolve_schema": {},
    }
    rows = await search_user_tools(ctx, "search Teams messages by sender", "outlook")
    names = [row["tool_name"] for row in rows]
    assert "TEAMS_LIST_CHATS" in names
    assert any(tool == "find_tools" and args.get("toolkit") == "microsoft_teams" for _, tool, args in calls)


async def test_mail_slug_stays_scoped_for_mail_search():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append((slug, tool))
        return {"candidates": []}

    outlook = _conn(slug="outlook", provider="outlook", kind="builtin", name="Outlook", alias="outlook")
    market = _conn(slug="composio", provider="composio", kind="builtin", name="Composio", alias="composio")
    ctx = _ctx([outlook, market], call_impl=fake_call)
    ctx.gateway_surface["composio"] = {
        "search": "find_tools",
        "search_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "execute": "execute",
        "resolve": None,
        "resolve_schema": {},
    }
    await search_user_tools(ctx, "search messages by sender", "outlook")
    assert calls == []


async def test_search_tools_expands_gateway_catalog():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append((tool, dict(args)))
        return {
            "candidates": [
                {"public_name": "gateway__tool_search", "description": "catalog", "score": 0.99},
                {"public_name": "get_sales_dashboard", "description": "KPIs and stock snapshot", "score": 0.4},
            ]
        }

    ctx = _ctx([_conn()], call_impl=fake_call)
    ctx.gateway_surface["digital"] = _gateway_surface()
    rows = await search_user_tools(ctx, "inventory left")
    names = [row["tool_name"] for row in rows]
    assert names[0] == "get_sales_dashboard"
    assert "gateway__tool_search" not in names
    assert rows[0]["execute_via"] == "gateway__execute"
    assert calls[0][0] == "gateway__tool_search"
    assert "inventory" in calls[0][1]["query"]


async def test_nested_tool_runs_through_execute():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append((tool, dict(args)))
        return {"on_hand": 16}

    ctx = _ctx([_conn()], call_impl=fake_call)
    ctx.gateway_surface["digital"] = _gateway_surface()
    out = await call_user_tool(ctx, "digital", "get_sales_dashboard", {"brand": "acme"})
    assert out["on_hand"] == 16
    assert calls[0][0] == "gateway__execute"
    assert calls[0][1]["tool_name"] == "get_sales_dashboard"
    assert calls[0][1]["arguments"]["brand"] == "acme"
    assert ctx.sources[-1]["source_tool"] == "digital__get_sales_dashboard"


async def test_catalog_search_query_is_rewritten():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append(dict(args))
        return {"candidates": []}

    ctx = _ctx([_conn()], call_impl=fake_call, query="inventory left")
    await call_user_tool(ctx, "digital", "gateway__tool_search", {"query": "please show remaining inventory"})
    assert "inventory" in calls[0]["query"]
    assert "please" not in calls[0]["query"]


async def test_catalog_search_is_not_stored_as_source():
    async def fake_call(slug, tool, args):
        return {"candidates": [{"public_name": "any_tool"}]}

    ctx = _ctx([_conn()], call_impl=fake_call)
    await call_user_tool(ctx, "digital", "gateway__tool_search", {"query": "sales"})
    assert ctx.used_tools == ["digital__gateway__tool_search"]
    assert ctx.sources == []


def test_compact_payload_keeps_a_map_not_a_chopped_string():
    huge = {"kpis": {"net_sales": 10}, "rows": [{"n": i, "label": f"row-{i}"} for i in range(5000)]}
    out = compact_payload(huge, 200)
    assert out["too_large"] is True
    assert out["map"]["rows"]["count"] == 5000
    assert "n" in out["map"]["rows"]["item_keys"]
    assert "preview" not in out


async def test_inspect_pages_the_asked_breakdown():
    rows = [{"style": f"S{i}", "net_sales": i} for i in range(250)]

    async def fake_call(slug, tool, args):
        return {"kpis": {"net_sales": {"value": 99}}, "breakdowns": {"by_style": rows}}

    ctx = _ctx([_conn()], call_impl=fake_call)
    first = await call_user_tool(ctx, "digital", "gateway__execute", {"tool_name": "get_sales_dashboard", "arguments": {}})
    assert ctx.results
    found = inspect_user_result(ctx, search="style", offset=0, limit=80)
    assert found["path"]
    assert found["data"]["total"] == 250
    assert found["data"]["items"][0]["style"] == "S0"
    assert found["data"]["next_offset"] == 80
    more = inspect_user_result(ctx, path=found["path"], offset=80, limit=80)
    assert more["data"]["items"][0]["style"] == "S80"
    if isinstance(first, dict) and first.get("too_large"):
        assert first.get("result_id")


def test_system_prompt_has_no_product_recipes():
    prompt = system_prompt(
        SimpleNamespace(timezone="Asia/Kolkata"),
        [_conn()],
        "",
    )
    lowered = prompt.lower()
    assert "list_connectors" in lowered
    assert "capability phrase" in lowered
    assert "gateway__tool_search return catalogs" in lowered
    assert "expands gateway catalogs" in lowered
    assert "cloud sandbox" in lowered or "composio connector is an app marketplace" in lowered
    assert "call_tool" in lowered
    assert "inspect_result" in lowered
    assert "schedule_task" in lowered
    assert "create_event" in lowered
    assert "create_google_meet" in lowered
    assert "create_teams_meeting" in lowered
    assert "publish_file" in lowered
    assert "connect_app" in lowered
    assert "connect card" in lowered
    assert "browse_open" in lowered or "browser_navigate" in lowered
    assert "browser_snapshot" in lowered
    assert "computer_act" in lowered
    assert "live computer" in lowered or "persistent chromium" in lowered or "shared live machine" in lowered
    assert "do not open the browser" in lowered
    assert "not in list_connections" in lowered
    assert "website without an api" in lowered
    assert "never say the computer or browser is unavailable" in lowered
    assert "flight" not in lowered
    assert "instagram" in lowered
    assert "web_read" in lowered and "do not use or wake the computer" in lowered
    assert "browser_search" in lowered and "which search key to configure" in lowered
    assert "browser_handoff" in lowered
    assert "fill whole forms" in lowered
    assert "never attach page.png" in lowered
    assert "google.com/search" in lowered
    assert "web_fetch" in lowered
    assert "never stop after one failed page" in lowered
    assert "never cite sandbox" in lowered
    assert "without connector_slug" in lowered
    assert "not outlook search_messages" in lowered
    assert "once you know the source" not in lowered
    assert "empty_folder" in lowered
    assert "get_thread" in lowered
    assert "never call get_thread with an empty thread_id" in lowered
    assert "detailed analyst" in lowered
    assert "markdown pipe tables" in lowered or "markdown tables" in lowered
    assert "download_url" in lowered
    assert "data analyst" in lowered
    assert "write_analytics_report" in lowered
    assert "save_skill" in lowered
    assert "web_search first" in lowered
    assert "first-class tools" in lowered
    assert "recall_memory" in lowered
    assert "scratchpad_add" in lowered
    assert "task_catalog" in lowered
    assert "skill_create" in lowered
    assert "request_takeover" in lowered
    assert "bot_wall" in lowered
    assert "do not invent" in lowered
    assert "destination site" in lowered
    assert "search_flights" not in lowered
    assert "start_teaching" in lowered
    assert "ten minutes" in lowered
    assert "microphone" in lowered
    assert "one computer-use task" in lowered
    assert "own screen" in lowered
    assert "line for a time series" in lowered
    assert "keep answers short" not in lowered
    assert "schedule_task and do not fetch now" in lowered
    assert "one8" not in lowered
    assert "lotto" not in lowered
    assert "grain_windows" not in lowered
    assert "@digital" in prompt


def test_agent_package_has_no_brand_date_heuristics():
    root = Path(__file__).resolve().parents[1] / "app" / "agent"
    banned = ("one8", "lotto", "grain_windows", "entity_slug", "month on month", "prefer_planned")
    for path in root.glob("*.py"):
        text = path.read_text().lower()
        for token in banned:
            assert token not in text, f"{path.name} contains {token}"


def test_harness_exposes_schedule_task():
    ctx = _ctx([_conn()])
    names = [tool.name for tool in harness_tools(ctx)]
    assert "schedule_task" in names
    schedule = next(tool for tool in harness_tools(ctx) if tool.name == "schedule_task")
    assert "future" in (schedule.description or "").lower()


FIRST_CLASS = [
    "computer_observe",
    "computer_act",
    "browser_navigate",
    "browser_snapshot",
    "browser_act",
    "shell",
    "user_computer_shell",
    "list_files",
    "read_file",
    "write_file",
    "attach_file",
    "open_path",
    "launch_app",
    "request_takeover",
    "web_search",
    "web_fetch",
    "render_plot",
    "generate_image",
    "generate_video",
    "remember",
    "save_memory",
    "save_shared_memory",
    "recall_memory",
    "forget_memory",
    "search_history",
    "read_history",
    "skill_read",
    "skill_create",
    "skill_update",
    "skill_delete",
    "scratchpad_list",
    "scratchpad_add",
    "scratchpad_update",
    "scratchpad_complete",
    "scratchpad_remove",
    "task_catalog",
]


def test_harness_exposes_first_class_computer_web_memory_tools():
    ctx = _ctx([_conn()])
    names = [tool.name for tool in harness_tools(ctx)]
    missing = [n for n in FIRST_CLASS if n not in names]
    assert missing == []


async def test_browser_navigate_harness_uses_computer_connector():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append((slug, tool, dict(args)))
        return {"ok": True, "url": args.get("url")}

    computer = _conn(slug="computer", provider="daytona", kind="builtin", name="Computer", alias="computer")
    ctx = _ctx([computer], call_impl=fake_call)
    nav = next(tool for tool in harness_tools(ctx) if tool.name == "browser_navigate")
    raw = await nav.coroutine(url="https://example.com")
    data = json.loads(raw)
    assert data["url"] == "https://example.com"
    assert calls == [("computer", "browser_navigate", {"url": "https://example.com"})]


async def test_user_computer_shell_does_not_use_daytona():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append(slug)
        return {"ok": True}

    computer = _conn(slug="computer", provider="daytona", kind="builtin", name="Computer", alias="computer")
    ctx = _ctx([computer], call_impl=fake_call)
    tool = next(t for t in harness_tools(ctx) if t.name == "user_computer_shell")
    data = json.loads(await tool.coroutine(command="ls"))
    assert data.get("error")
    assert "local" in data["error"].lower()
    assert calls == []


async def test_request_takeover_is_browser_handoff():
    computer = _conn(slug="computer", provider="daytona", kind="builtin", name="Computer", alias="computer")
    ctx = _ctx([computer])
    tool = next(t for t in harness_tools(ctx) if t.name == "request_takeover")
    data = json.loads(await tool.coroutine(reason="Log in to the site."))
    assert data["handoff"] is True
    assert ctx.pending[0]["kind"] == "handoff"


async def test_bot_wall_auto_handoff_on_navigate():
    async def fake_call(slug, tool, args):
        return {
            "ok": True,
            "url": "https://www.google.com/sorry/index",
            "title": "Sorry",
            "text": "Our systems have detected unusual traffic from your computer.",
        }

    computer = _conn(slug="computer", provider="daytona", kind="builtin", name="Computer", alias="computer")
    ctx = _ctx([computer], call_impl=fake_call)
    nav = next(tool for tool in harness_tools(ctx) if tool.name == "browser_navigate")
    data = json.loads(await nav.coroutine(url="https://www.google.com/search?q=news"))
    assert data["bot_wall"] is True
    assert data.get("handoff") is True
    assert any(isinstance(p, dict) and p.get("kind") == "handoff" for p in ctx.pending)


def test_last_text_skips_tool_calls():
    ai = SimpleNamespace(type="ai", tool_calls=[{"name": "call_tool"}], content="")
    final = SimpleNamespace(type="ai", tool_calls=[], content="Net sales were 12.")
    assert last_text([ai, final]) == "Net sales were 12."


async def test_open_mail_hydrates_full_thread():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append((tool, dict(args)))
        if tool == "search_messages":
            return {"messages": [{"message_id": "AAMk1", "thread_id": "", "subject": "Re: Adidas"}]}
        if tool == "get_thread":
            return {
                "thread_id": "AAQkConv",
                "subject": "Adidas",
                "message_count": 2,
                "last_message_id": "AAMk1",
                "messages": [
                    {"message_id": "m0", "from": "Regina", "body": "Please find the profiles."},
                    {"message_id": "AAMk1", "from": "Gourishanker", "body": "Please take this forward."},
                ],
            }
        return {}

    conn = _conn(slug="outlook", provider="outlook", name="Outlook", alias="outlook")
    ctx = _ctx([conn], call_impl=fake_call, query="open this mail thread Adidas Data Engineer")
    result = await call_user_tool(ctx, "outlook", "search_messages", {"query": "Adidas Data Engineer"})
    assert result["opened_thread"]["message_count"] == 2
    assert result["thread_id"] == "AAQkConv"
    assert result["message_id"] == "AAMk1"
    assert any(tool == "get_thread" and args.get("message_id") == "AAMk1" for tool, args in calls)
    assert "outlook__get_thread" in ctx.used_tools


async def test_get_thread_fills_empty_id_from_prior_search():
    async def fake_call(slug, tool, args):
        if tool == "search_messages":
            return {"messages": [{"message_id": "m1", "thread_id": "t1", "subject": "Hi"}]}
        if tool == "get_thread":
            return {
                "thread_id": args.get("thread_id") or "t1",
                "message_count": 1,
                "last_message_id": args.get("message_id") or "m1",
                "messages": [{"message_id": "m1", "body": "hi"}],
            }
        return {}

    conn = _conn(slug="outlook", provider="outlook", name="Outlook", alias="outlook")
    ctx = _ctx([conn], call_impl=fake_call, query="find Adidas profiles")
    await call_user_tool(ctx, "outlook", "search_messages", {"query": "Adidas profiles"})
    out = await call_user_tool(ctx, "outlook", "get_thread", {"thread_id": ""})
    assert out["thread_id"] == "t1"
    assert out["last_message_id"] == "m1"
