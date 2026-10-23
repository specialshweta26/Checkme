from app.routing.retrieve import (
    capability_query,
    embedding_query,
    is_catalog_tool,
    lexical_tsquery,
    retrieve_score,
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
