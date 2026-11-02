from types import SimpleNamespace

import pytest
from connector_core import ToolError

from app.connectors.composio_box import COMPOSIO_TOOLS, slim_tool
from app.connectors.computer_daemon import (
    VERSION as DAEMON_VERSION,
    annotate_page,
    bot_wall,
    page_is_empty,
    page_is_thin,
    should_paint_desktop,
)
from app.connectors.daytona_box import DAYTONA_TOOLS, decode_screen_b64, live_screen_png, remember_screen, safe_http_url
from app.connectors.platform import PLATFORM
from app.routing.describe import catalog_lines
from app.routing.retrieve import GATEWAY_EXECUTE_NAMES, GATEWAY_SEARCH_NAMES, is_catalog_tool, wrap_through_execute


def test_platform_tool_sets():
    assert {s.name for s in DAYTONA_TOOLS} >= {
        "exec",
        "run_code",
        "read_file",
        "write_file",
        "list_dir",
        "sandbox_info",
        "publish_file",
        "write_analytics_report",
        "browse_open",
        "browse_act",
        "browse_screenshot",
        "browser_navigate",
        "browser_snapshot",
        "browser_act",
        "computer_observe",
        "computer_act",
        "browser_handoff",
        "web_fetch",
    }
    names = {s.name for s in COMPOSIO_TOOLS}
    assert names >= {"find_tools", "execute", "resolve", "list_apps", "connect_app", "list_connections"}
    assert PLATFORM["daytona"]["slug"] == "computer"
    assert PLATFORM["composio"]["slug"] == "composio"


def test_browser_act_covers_forms_and_handoff():
    spec = next(s for s in DAYTONA_TOOLS if s.name == "browser_act")
    low = spec.description.lower()
    assert "fill" in low
    assert "select" in low
    assert "pick" in low
    assert "upload" in low and "download" in low
    assert "type-ahead" in low or "autocomplete" in low or "combobox" in low
    handoff = next(s for s in DAYTONA_TOOLS if s.name == "browser_handoff")
    hlow = handoff.description.lower()
    assert "login" in hlow
    assert "dropdown" in hlow or "autocomplete" in hlow


def test_browser_urls_must_be_http():
    assert safe_http_url("example.com/path") == "https://example.com/path"
    assert safe_http_url("https://news.ycombinator.com/") == "https://news.ycombinator.com/"
    with pytest.raises(ToolError):
        safe_http_url("javascript:alert(1)")
    with pytest.raises(ToolError):
        safe_http_url("file:///etc/passwd")


def test_composio_gateway_names_match_indexed_tools():
    assert "find_tools" in GATEWAY_SEARCH_NAMES
    assert "execute" in GATEWAY_EXECUTE_NAMES
    assert is_catalog_tool("find_tools")
    assert is_catalog_tool("list_apps")
    name, args = wrap_through_execute("GITHUB_CREATE_AN_ISSUE", {"title": "Bug"}, "execute")
    assert name == "execute"
    assert args["tool_name"] == "GITHUB_CREATE_AN_ISSUE"
    assert args["arguments"]["title"] == "Bug"


def test_slim_tool_uses_slug_as_public_name():
    row = slim_tool(
        {
            "slug": "GITHUB_CREATE_AN_ISSUE",
            "name": "Create an issue",
            "description": "Open a GitHub issue",
            "toolkit": {"slug": "github"},
        }
    )
    assert row["public_name"] == "GITHUB_CREATE_AN_ISSUE"
    assert row["toolkit"] == "github"


def test_catalog_lines_label_computer_and_marketplace():
    computer = SimpleNamespace(
        slug="computer",
        alias=None,
        name="Computer",
        description="sandbox",
        kind="builtin",
        provider="daytona",
    )
    market = SimpleNamespace(
        slug="composio",
        alias=None,
        name="Composio",
        description="apps",
        kind="builtin",
        provider="composio",
    )
    lines = catalog_lines([computer, market])
    assert "computer" in lines
    assert "marketplace" in lines
    assert "exec" in lines
    assert "publish_file" in lines
    assert "write_analytics_report" in lines
    assert "browser_navigate" in lines
    assert "browser_handoff" in lines
    assert "computer_act" in lines
    assert "find_tools" in lines
    assert "list_connections" in lines


def test_daemon_version_is_current():
    assert DAEMON_VERSION == "21"


def test_bot_wall_detects_sorry_and_skips_cookie_consent():
    sorry = bot_wall("https://www.google.com/sorry/index", "Sorry", "unusual traffic from your computer")
    assert sorry and sorry["bot_wall"] is True
    assert bot_wall("https://www.google.com/", "Google", "Before you continue to Google") is None
    assert page_is_thin("Loading") is True
    ready = annotate_page(
        {
            "url": "https://example.com/results",
            "text": "Results\n" + ("Nonstop 6h 10m ₹12,450 departs 08:15 am\n" * 8),
        }
    )
    assert ready["content_ready"] is True
    wall = annotate_page({"url": "https://www.skyscanner.net/", "text": "Please verify you are a human. I'm not a robot."})
    assert wall["bot_wall"] is True
    assert wall["ok"] is False


def test_blank_and_chrome_error_pages_count_as_empty():
    assert page_is_empty("about:blank")
    assert page_is_empty("chrome-error://chromewebdata/")
    assert page_is_empty("")
    assert not page_is_empty("https://www.google.com/travel/flights")


def test_fetched_html_on_blank_tab_is_not_wiped():
    article = "<body>" + ("Disha Patani Instagram " * 20) + "</body>"
    assert not should_paint_desktop("about:blank", "Disha Patani", article)
    assert not should_paint_desktop("about:blank", "Computer", "Computer is on")
    assert not should_paint_desktop("https://www.instagram.com/dishapatani/", "Instagram", "")
    assert should_paint_desktop("about:blank", "", "")
    assert should_paint_desktop("chrome-error://chromewebdata/", "Error", "")


def test_live_screen_cache_keeps_last_good_png():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    remember_screen(png)
    assert live_screen_png() == png
    remember_screen(b"not a png")
    assert live_screen_png() == png


def test_decode_screen_b64_requires_png():
    import base64

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    assert decode_screen_b64(base64.b64encode(png).decode()) == png
    with pytest.raises(ToolError):
        decode_screen_b64("not-png")
    with pytest.raises(ToolError):
        decode_screen_b64("")
