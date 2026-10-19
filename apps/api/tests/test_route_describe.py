from types import SimpleNamespace

from app.routing.describe import catalog_lines, description_score, mentioned_connectors


def _conn(**kwargs):
    defaults = {
        "id": "1",
        "name": "mcp-gateway",
        "alias": "digital",
        "slug": "digital",
        "description": "One8 daily sales, retail KPIs, Agilitas digital",
        "tags": ["sales"],
        "kind": "mcp",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_from_digital_picks_alias():
    digital = _conn()
    gmail = _conn(id="2", name="Gmail", alias=None, slug="gmail", description="Inbox and calendar", tags=["mail"], kind="builtin")
    hits = mentioned_connectors("one8 sales today from digital", [gmail, digital])
    assert [c.slug for c in hits] == ["digital"]


def test_description_beats_mail_for_sales():
    digital = _conn()
    gmail = _conn(id="2", name="Gmail", alias=None, slug="gmail", description="Inbox and calendar", tags=["mail"], kind="builtin")
    assert description_score("one8 sales today from digital", digital) > description_score("one8 sales today from digital", gmail)


def test_catalog_is_per_user_connectors_only():
    lines = catalog_lines([_conn(), _conn(id="2", name="Gmail", alias=None, slug="gmail", description="", kind="builtin")])
    assert "@digital" in lines
    assert "One8 daily sales" in lines
    assert "@gmail" in lines
