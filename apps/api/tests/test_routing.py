from app.routing.retrieve import (
    capability_query,
    embedding_query,
    gateway_search_query,
    is_catalog_tool,
    lexical_tsquery,
    nested_tool_rows,
    retrieve_score,
    wrap_through_execute,
)
from app.routing.split import explicit_slugs, first_party_tag


def test_explicit_slug():
    assert explicit_slugs("search @notion for the brief") == ["notion"]


def test_first_party():
    assert first_party_tag("what's on my calendar tomorrow")[1] == "calendar"
    assert first_party_tag("summarize the hiring thread")[0] == "thread_summary"


def test_capability_query_drops_filler_keeps_skill():
    assert capability_query("one8 inventory left") == "one8 inventory"
    assert "left" not in capability_query("show me stock left")


def test_embedding_query_omits_entity_codes():
    q = embedding_query("one8 inventory left")
    assert "inventory" in q
    assert "one8" not in q
    assert q.startswith("Find a connector tool for:")


def test_lexical_tsquery_is_or_prefix_not_and():
    q = lexical_tsquery("one8 inventory left")
    assert "inventory:*" in q
    assert " | " in q
    assert "&" not in q


def test_catalog_tools_are_penalized():
    assert is_catalog_tool("gateway__tool_search")
    data = retrieve_score("get_product_performance", 0.8, ["inventory"])
    catalog = retrieve_score("gateway__tool_search", 0.8, ["inventory"])
    assert data > catalog


def test_gateway_search_query_omits_codes_and_expands_stock():
    q = gateway_search_query("one8 inventory left")
    assert "inventory" in q
    assert "stock" in q
    assert "one8" not in q


def test_wrap_through_execute_nests_data_tools_only():
    name, args = wrap_through_execute("get_sales_dashboard", {"brand": "acme"}, "gateway__execute")
    assert name == "gateway__execute"
    assert args == {"tool_name": "get_sales_dashboard", "arguments": {"brand": "acme"}}
    search_name, search_args = wrap_through_execute("gateway__tool_search", {"query": "inventory"}, "gateway__execute")
    assert search_name == "gateway__tool_search"
    already_name, already_args = wrap_through_execute(
        "gateway__execute",
        {"tool_name": "get_sales_dashboard", "arguments": {}},
        "gateway__execute",
    )
    assert already_name == "gateway__execute"
    assert already_args["tool_name"] == "get_sales_dashboard"


def test_nested_tool_rows_drop_catalog_hits():
    rows = nested_tool_rows(
        "digital",
        "digital",
        [
            {"public_name": "gateway__tool_search", "description": "catalog", "score": 0.99},
            {"public_name": "get_sales_dashboard", "description": "stock snapshot", "score": 0.4},
        ],
        "gateway__execute",
        ["inventory", "stock"],
    )
    assert [row["tool_name"] for row in rows] == ["get_sales_dashboard"]
    assert rows[0]["execute_via"] == "gateway__execute"
