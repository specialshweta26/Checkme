from app.connectors.composio_box import _auth_kind, _initiation, _matches_query, _toolkit_row


def test_auth_kind_oauth_vs_api_key():
    assert (
        _auth_kind({"auth_schemes": ["OAUTH2"], "composio_managed_auth_schemes": ["OAUTH2"], "no_auth": False})
        == "oauth"
    )
    assert _auth_kind({"auth_schemes": ["API_KEY"], "composio_managed_auth_schemes": [], "no_auth": False}) == "api_key"
    assert _auth_kind({"no_auth": True, "auth_schemes": []}) == "none"


def test_toolkit_row_marks_serpapi_as_api_key():
    row = _toolkit_row({"slug": "serpapi", "name": "SerpApi", "auth_schemes": ["API_KEY"], "composio_managed_auth_schemes": [], "meta": {}})
    assert row["auth_kind"] == "api_key"


def test_initiation_reads_generic_api_key_field():
    scheme, fields = _initiation(
        {
            "composio_managed_auth_schemes": [],
            "auth_config_details": [
                {
                    "mode": "API_KEY",
                    "fields": {
                        "connected_account_initiation": {
                            "required": [{"name": "generic_api_key", "displayName": "API Key", "is_secret": True}]
                        }
                    },
                }
            ],
        }
    )
    assert scheme == "API_KEY"
    assert fields[0]["name"] == "generic_api_key"


def test_initiation_falls_back_to_auth_kind():
    scheme, fields = _initiation({"auth_schemes": ["API_KEY"], "composio_managed_auth_schemes": []})
    assert scheme == "API_KEY"
    assert fields[0]["name"] == "generic_api_key"


def test_matches_query_uses_slug_name_and_description():
    row = {"slug": "serpapi", "name": "SerpApi", "description": "search results"}
    assert _matches_query(row, "serp")
    assert _matches_query(row, "RESULTS")
    assert not _matches_query(row, "gmail")
