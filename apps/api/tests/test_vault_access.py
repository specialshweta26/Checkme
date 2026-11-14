"""Vault sign-ins belong to the agents they were given to (app/browser_runs/logins.py "Access").

The cloud browser's cookies are shared by every agent, so an agent that wasn't given a site's sign-in must not reach
that site at all: not with the password, and not through a session another agent signed in to."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.agent import computer_harness as ch
from app.browser_runs import logins

RAAZ = SimpleNamespace(id=uuid.uuid4(), name="Raaz")
MARKI = SimpleNamespace(id=uuid.uuid4(), name="Marki")
USER = SimpleNamespace(id=uuid.uuid4())
RULES = [{"host": "portal.acme.com", "label": "Acme portal", "agent_ids": [str(RAAZ.id)]}]


def test_only_the_agents_given_a_sign_in_may_open_its_site():
    assert logins.blocked_rule(RULES, "https://portal.acme.com/reports", RAAZ.id) is None
    assert logins.blocked_rule(RULES, "https://portal.acme.com/reports", MARKI.id)["host"] == "portal.acme.com"
    assert logins.blocked_rule(RULES, "https://sso.portal.acme.com/login", MARKI.id) is not None  # subdomains too
    assert logins.blocked_rule(RULES, "https://portal.acme.com/", None) is not None  # a run with no agent
    assert logins.blocked_rule(RULES, "https://acme.com/", MARKI.id) is None  # the parent site has no saved sign-in
    assert logins.blocked_rule(RULES, "https://news.example.com/", MARKI.id) is None
    nobody = [{"host": "portal.acme.com", "label": "", "agent_ids": []}]
    assert logins.blocked_rule(nobody, "https://portal.acme.com/", RAAZ.id) is not None


def test_the_refusal_names_the_agent_and_where_to_allow_it():
    out = logins.refusal(RULES[0], "Marki")
    assert out["ok"] is False and out["blocked"] == "vault"
    assert "Marki isn't allowed" in out["error"] and "Profile > Vault" in out["error"] and "Do not try another way in" in out["error"]


@pytest.fixture
def browser(monkeypatch):
    async def rules(user_id):
        return RULES

    monkeypatch.setattr(logins, "rules", rules)
    sent = []
    page = {"url": "https://portal.acme.com/home", "title": "Acme portal", "text": "Q3 revenue 4.2M"}

    async def run(ctx, tool, args, force_cloud=False):
        sent.append((tool, dict(args or {})))
        return dict(page)

    async def tab_for(ctx, tool, args, where, plain):
        return None

    monkeypatch.setattr(ch, "_run_computer", run)
    monkeypatch.setattr(ch.browser_tabs, "tab_for", tab_for)
    monkeypatch.setattr(ch, "browser_local", lambda ctx: False)
    return SimpleNamespace(sent=sent, page=page)


def _ctx(agent):
    return SimpleNamespace(user=USER, agent=agent, observed=[], popups=[], used_tools=[], session_id="s1")


async def test_an_agent_without_the_sign_in_cannot_open_the_site(browser):
    out = await ch.run_computer(_ctx(MARKI), "browser_navigate", {"url": "https://portal.acme.com/reports"})
    assert out["blocked"] == "vault" and browser.sent == []  # never even sent to the browser


async def test_a_click_that_lands_on_the_site_returns_nothing_from_the_page(browser):
    browser.page.update(url="https://portal.acme.com/home")
    out = await ch.run_computer(_ctx(MARKI), "browser_act", {"actions": [{"kind": "click", "ref": "e3"}]})
    assert out["blocked"] == "vault"
    assert "Q3 revenue" not in json.dumps(out)


async def test_the_agent_given_the_sign_in_works_as_before(browser):
    out = await ch.run_computer(_ctx(RAAZ), "browser_navigate", {"url": "https://portal.acme.com/reports"})
    assert out.get("text") == "Q3 revenue 4.2M" and browser.sent[0][0] == "browser_navigate"


async def test_other_sites_are_untouched(browser):
    browser.page.update(url="https://news.example.com/", text="headlines")
    out = await ch.run_computer(_ctx(MARKI), "browser_navigate", {"url": "https://news.example.com/"})
    assert out.get("text") == "headlines"


async def test_browser_task_refuses_before_starting(browser, monkeypatch):
    from app.agent import fast_browser as fb

    out = await fb._run_task(_ctx(MARKI), "get this week's revenue", "https://portal.acme.com/reports", None, 0, False, True, True, {})
    assert out["blocked"] == "vault" and browser.sent == []


async def test_the_password_goes_only_to_the_agent_given_it(monkeypatch):
    from app.agent import fast_browser as fb

    seen = []

    async def values_for(user_id, host, agent_id=None):
        seen.append(agent_id)
        return {logins.USERNAME: "ops@acme.com", logins.PASSWORD: "pw"} if agent_id == RAAZ.id else {}

    monkeypatch.setattr(logins, "values_for", values_for)
    assert await fb.add_saved_login(USER.id, "portal.acme.com", {}, RAAZ.id)
    assert not await fb.add_saved_login(USER.id, "portal.acme.com", {}, MARKI.id)
    assert seen == [RAAZ.id, MARKI.id]


def test_existing_sign_ins_start_with_the_agents_whose_skills_use_the_site():
    taught = SimpleNamespace(instruction="", steps=[{"kind": "navigate", "url": "https://portal.acme.com/login"}], source={})
    written = SimpleNamespace(instruction="Open portal.acme.com and export the weekly report.", steps=[], source={})
    other = SimpleNamespace(instruction="Post the weekly update on LinkedIn.", steps=[], source={})
    assert logins.skill_mentions("portal.acme.com", taught)
    assert logins.skill_mentions("portal.acme.com", written)
    assert not logins.skill_mentions("portal.acme.com", other)
    assert logins.allowed(SimpleNamespace(agent_ids=[str(RAAZ.id)]), RAAZ.id)
    assert not logins.allowed(SimpleNamespace(agent_ids=[str(RAAZ.id)]), MARKI.id)
    assert not logins.allowed(SimpleNamespace(agent_ids=None), RAAZ.id)
