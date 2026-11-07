"""Memory stays current and bounded: facts are replaced instead of piling up, each fact is stored once, core blocks
are rewritten to a fixed size, and recall reads a bounded number of rows."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.agent import memory, memory_extract
from app.agent.memory import SCOPE_AGENT, SCOPE_SHARED, STATUS_ACTIVE, STATUS_SUPERSEDED
from app.models import MemoryPassage, User


def _user():
    return SimpleNamespace(id=uuid.uuid4(), display_name="Ada", email="a@x.com", timezone="Asia/Kolkata")


def _agent(user):
    return SimpleNamespace(id=uuid.uuid4(), user_id=user.id, name="Home", kind="custom", letta_agent_id=None)


def _fact(fid, user, agent, text, *, scope=SCOPE_AGENT, status=STATUS_ACTIVE, days_old=1):
    return SimpleNamespace(
        id=fid,
        user_id=user.id,
        agent_id=agent.id,
        text=text,
        scope=scope,
        status=status,
        kind="archival",
        created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
    )


# --- supersede (M1) ---------------------------------------------------------------------------------------------


def test_supersede_resolves_only_facts_the_extractor_was_shown():
    known = [(12, "Lives in Bangalore"), (13, "Works at Acme as an engineer")]
    assert memory_extract._resolve_supersede(12, known) == 12
    assert memory_extract._resolve_supersede("#13", known) == 13
    assert memory_extract._resolve_supersede("lives in bangalore.", known) == 12
    assert memory_extract._resolve_supersede("Works at Acme", known) == 13
    assert memory_extract._resolve_supersede(99, known) is None  # not shown, never retired
    assert memory_extract._resolve_supersede("unrelated text here", known) is None
    assert memory_extract._resolve_supersede("", known) is None
    assert memory_extract._resolve_supersede(12, []) is None


@pytest.mark.asyncio
async def test_apply_extract_replaces_the_old_fact_and_skips_known_ones(monkeypatch):
    user, agent = _user(), _agent(_user())
    calls = []

    async def fake_remember(_db, user_id, kind, text, agent_id, scope=SCOPE_AGENT, **kwargs):
        calls.append((text, kwargs.get("supersedes_id")))

    monkeypatch.setattr(memory_extract, "remember", fake_remember)
    known = [(12, "Lives in Bangalore"), (13, "Prefers tea")]
    out = await memory_extract.apply_extract(
        None,
        user,
        agent,
        {
            "facts": [
                {"text": "Lives in Pune", "scope": "user_shared", "supersede": 12},
                {"text": "Prefers tea", "scope": "agent"},  # already known: not stored again
                {"text": "Moved to Pune in October", "supersede": "12"},  # 12 is already retired this turn
                {"text": "Has a dog", "supersede": 404},  # unknown id: stored, nothing retired
            ]
        },
        known=known,
    )
    assert calls == [("Lives in Pune", 12), ("Moved to Pune in October", None), ("Has a dog", None)]
    assert out["facts"] == 3 and out["superseded"] == 1


@pytest.mark.asyncio
async def test_extract_prompt_lists_known_facts_by_id(monkeypatch):
    seen = {}

    async def fake_complete(messages, **_kwargs):
        seen["prompt"] = messages[0]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"facts": []}'))])

    monkeypatch.setattr(memory_extract, "chat_complete", fake_complete)
    await memory_extract._extract_json("I moved to Pune", "Noted.", [(12, "Lives in Bangalore")])
    assert "12: Lives in Bangalore" in seen["prompt"]
    assert "supersede is the id" in seen["prompt"]


class _RememberDB:
    def __init__(self, user, rows):
        self.user = user
        self.rows = {r.id: r for r in rows}
        self.added = []

    async def get(self, model, ident):
        if model is User:
            return self.user
        if model is MemoryPassage:
            return self.rows.get(ident)
        return None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        return None


def _quiet_remember(monkeypatch, owner, forgotten):
    async def owner_for(*_a, **_k):
        return owner

    async def nothing(*_a, **_k):
        return None

    async def vec(_text):
        return [0.0]

    async def forget(agent, snippet):
        forgotten.append(snippet)

    monkeypatch.setattr(memory, "_memory_agent", owner_for)
    monkeypatch.setattr(memory, "_ensure_saved_letta", nothing)
    monkeypatch.setattr(memory, "_write_letta_passage", nothing)
    monkeypatch.setattr(memory, "_forget_letta_text", forget)
    monkeypatch.setattr(memory, "embed_one", vec)


@pytest.mark.asyncio
async def test_remember_retires_the_replaced_fact_and_writes_no_second_copy(monkeypatch):
    user = _user()
    agent = _agent(user)
    old = _fact(12, user, agent, "Lives in Bangalore")
    forgotten = []
    _quiet_remember(monkeypatch, agent, forgotten)

    async def must_not_copy(*_a, **_k):
        raise AssertionError("facts are stored once, in memory_passages")

    monkeypatch.setattr("app.memory_engine.semantic.store_document", must_not_copy)
    db = _RememberDB(user, [old])
    row = await memory.remember(db, user.id, "archival", "Lives in Pune", agent.id, supersedes_id=12)
    assert old.status == STATUS_SUPERSEDED
    assert row.supersedes_id == 12
    assert forgotten == ["Lives in Bangalore"]
    assert [type(o) for o in db.added] == [MemoryPassage]


@pytest.mark.asyncio
async def test_remember_will_not_retire_another_agents_fact(monkeypatch):
    user = _user()
    mine, theirs = _agent(user), _agent(user)
    other = _fact(12, user, theirs, "Lives in Bangalore")
    forgotten = []
    _quiet_remember(monkeypatch, mine, forgotten)
    db = _RememberDB(user, [other])
    row = await memory.remember(db, user.id, "archival", "Lives in Pune", mine.id, supersedes_id=12)
    assert other.status == STATUS_ACTIVE
    assert row.supersedes_id is None
    assert forgotten == []


# --- core blocks (M3) -------------------------------------------------------------------------------------------


def _llm_reply(monkeypatch, text=None, error=None):
    async def fake(messages, **_kwargs):
        if error:
            raise error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    monkeypatch.setattr("app.llm.chat_complete", fake)


@pytest.mark.asyncio
async def test_merge_block_starts_empty_blocks_and_skips_repeats(monkeypatch):
    _llm_reply(monkeypatch, error=AssertionError("no rewrite needed"))
    assert await memory.merge_block("prefs", "", "Short answers") == ("Short answers", True)
    assert await memory.merge_block("prefs", "Short answers\nTables", "short answers") == ("Short answers\nTables", False)


@pytest.mark.asyncio
async def test_merge_block_rewrites_conflicts_instead_of_appending(monkeypatch):
    _llm_reply(monkeypatch, "- Detailed answers\n- Markdown tables")
    value, changed = await memory.merge_block("prefs", "Short answers\nMarkdown tables", "Detailed answers")
    assert changed is True
    assert value == "Detailed answers\nMarkdown tables"


@pytest.mark.asyncio
async def test_merge_block_falls_back_to_newest_lines_within_the_cap(monkeypatch):
    _llm_reply(monkeypatch, error=RuntimeError("model down"))
    prior = "\n".join(f"note {i} " + "x" * 80 for i in range(20))
    value, changed = await memory.merge_block("prefs", prior, "Newest preference")
    max_chars, max_lines = memory.BLOCK_LIMITS["prefs"]
    assert changed is True
    assert len(value) <= max_chars and len(value.splitlines()) <= max_lines
    assert value.splitlines()[-1] == "Newest preference"
    assert "note 0 " not in value  # the oldest go first


@pytest.mark.asyncio
async def test_merge_block_rejects_a_rewrite_that_mentions_secrets(monkeypatch):
    _llm_reply(monkeypatch, "Short answers\nTheir password is hunter2")
    value, _ = await memory.merge_block("prefs", "Short answers", "Use tables")
    assert "password" not in value
    assert value == "Short answers\nUse tables"


def test_fit_block_keeps_the_later_copy_of_a_repeat():
    assert memory._fit_block(["A", "B", "a", "C"], 100, 10) == "B\na\nC"
    assert memory._fit_block(["one", "two", "three"], 100, 2) == "two\nthree"


@pytest.mark.asyncio
async def test_remember_fact_rewrites_core_blocks(monkeypatch):
    user = _user()
    agent = _agent(user)
    saved = {}

    async def read(*_a, **_k):
        return "Short answers"

    async def upsert(_db, _uid, label, value, _aid):
        saved[label] = value

    monkeypatch.setattr(memory, "read_core", read)
    monkeypatch.setattr(memory, "upsert_core", upsert)
    _llm_reply(monkeypatch, "Detailed answers")
    ctx = SimpleNamespace(db=object(), user=user, agent=agent)
    out = await memory.remember_fact(ctx, "Detailed answers", label="prefs")
    assert out["ok"] is True
    assert saved["prefs"] == "Detailed answers"


# --- bounded recall (M5) ----------------------------------------------------------------------------------------


def test_tsquery_is_or_of_safe_words():
    assert memory._tsquery("What's my dentist's name & phone?") == "dentist | name | phone"
    assert memory._tsquery("the and") == ""


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return [(r,) for r in self._rows]

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


class _RecallDB:
    """Answers the three candidate-pool queries and the final load, and records each query."""

    def __init__(self, rows, vec_ids, kw_ids):
        self.rows = {r.id: r for r in rows}
        self.vec_ids, self.kw_ids = vec_ids, kw_ids
        self.queries = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.queries.append((sql, params or {}))
        if params and "vec" in params:
            return _Rows(self.vec_ids)
        if params and "tsq" in params:
            return _Rows(self.kw_ids)
        compiled = stmt.compile()
        if "ORDER BY memory_passages.id DESC" in sql:
            lim = compiled.params.get("param_1", 8)
            aid = compiled.params.get("agent_id_1")
            newest = sorted(self.rows.values(), key=lambda r: -r.id)
            visible = [r for r in newest if r.status == STATUS_ACTIVE and (r.agent_id == aid or r.scope == SCOPE_SHARED)]
            return _Rows(visible[:lim])
        wanted = next(v for v in compiled.params.values() if isinstance(v, list))
        return _Rows([self.rows[i] for i in wanted if i in self.rows])


@pytest.mark.asyncio
async def test_ranked_facts_reads_bounded_pools_and_respects_scope(monkeypatch):
    user = _user()
    agent, other = _agent(user), _agent(user)

    async def vec(_text):
        return [0.1, 0.2]

    monkeypatch.setattr(memory, "embed_one", vec)
    rows = [_fact(i, user, agent, f"filler note {i}", days_old=200) for i in range(1, 30)]
    rows.append(_fact(40, user, agent, "Dentist is Dr Rao, phone 98450", days_old=300))
    rows.append(_fact(41, user, other, "Dentist secret of another agent", days_old=1))
    rows.append(_fact(42, user, other, "Dentist shared with every agent", scope=SCOPE_SHARED, days_old=2))
    db = _RecallDB(rows, vec_ids=[40, 41, 42], kw_ids=[40, 41])
    facts = await memory._ranked_facts(db, user.id, agent.id, "who is my dentist", limit=5)
    ids = [r.id for r in facts]
    assert ids[0] == 40  # best vector and keyword match beats newer filler
    assert 42 in ids  # user-shared is visible
    assert 41 not in ids  # another agent's private fact is not
    assert len(facts) == 5
    pools = [p for _sql, p in db.queries if "lim" in p]
    assert pools and all(p["lim"] == memory.FACT_POOL for p in pools)
    assert any(p.get("tsq") == "who | dentist" for p in pools)


@pytest.mark.asyncio
async def test_ranked_facts_survives_a_failed_vector_search(monkeypatch):
    user = _user()
    agent = _agent(user)

    async def down(_text):
        raise RuntimeError("embeddings down")

    monkeypatch.setattr(memory, "embed_one", down)
    rows = [_fact(1, user, agent, "Prefers tea"), _fact(2, user, agent, "Old", status=STATUS_SUPERSEDED)]
    db = _RecallDB(rows, vec_ids=[], kw_ids=[1])
    facts = await memory._ranked_facts(db, user.id, agent.id, "tea", limit=8)
    assert [r.id for r in facts] == [1]


# --- one copy per fact (M2) -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_knowledge_recall_skips_legacy_fact_copies(monkeypatch):
    from app.memory_engine import semantic

    async def vec(_text):
        return [0.1]

    monkeypatch.setattr(semantic, "_embed", vec)
    seen = []

    class DB:
        async def execute(self, stmt, params=None):
            seen.append(str(stmt))
            return _Rows([])

    await semantic.search_documents(DB(), uuid.uuid4(), uuid.uuid4(), "dentist")
    assert "source <> 'fact'" in seen[0]
    documents = next(sql for sql in seen if "FROM semantic_documents" in sql and "semantic_chunks" not in sql)
    assert "semantic_documents.source !=" in documents


@pytest.mark.asyncio
async def test_learned_block_replaces_an_outdated_line(monkeypatch):
    """remember_for_me writes the 'learned' block: a move replaces the old city instead of being appended."""
    user = _user()
    agent = _agent(user)
    saved = {}

    async def read(*_a, **_k):
        return "- work: Lives in Bangalore and works at Acme\n- routine: Gym on Tuesdays"

    async def upsert(_db, _uid, label, value, _aid):
        saved[label] = value

    monkeypatch.setattr(memory, "read_core", read)
    monkeypatch.setattr(memory, "upsert_core", upsert)
    _llm_reply(monkeypatch, "work: Lives in Pune and works at Acme\nroutine: Gym on Tuesdays")
    out = await memory.learn_for_agent(object(), user, agent, "work", "Moved to Pune, still at Acme")
    assert out["saved"] is True
    assert "Bangalore" not in saved["learned"] and "Pune" in saved["learned"]
    assert "Gym on Tuesdays" in saved["learned"]


@pytest.mark.asyncio
async def test_learned_block_has_no_size_limit(monkeypatch):
    """The learned block is uncapped: even with the model down, nothing is trimmed for length."""
    assert memory.BLOCK_LIMITS["learned"] == (None, None)
    _llm_reply(monkeypatch, error=RuntimeError("model down"))
    prior = "\n".join(f"note {i} " + "x" * 80 for i in range(40))
    value, changed = await memory.merge_block("learned", prior, "Newest note")
    assert changed is True
    assert len(value.splitlines()) == 41 and "note 0 " in value and value.endswith("Newest note")
    long_first = "y" * 5000
    assert await memory.merge_block("learned", "", long_first) == (long_first, True)


@pytest.mark.asyncio
async def test_a_newer_note_clears_what_it_contradicts_in_related_blocks(monkeypatch):
    """'detailed answers' saved to voice removes 'short one-line answers' from prefs; unrelated lines stay."""
    user = _user()
    agent = _agent(user)
    blocks = {"prefs": "Keep answers short and one-line\nUse Markdown tables for comparisons", "learned": "", "voice": "x"}

    async def read(_db, _user, label, _agent=None):
        return blocks.get(label, "")

    async def upsert(_db, _uid, label, value, _aid):
        blocks[label] = value

    monkeypatch.setattr(memory, "read_core", read)
    monkeypatch.setattr(memory, "upsert_core", upsert)
    _llm_reply(monkeypatch, "Use Markdown tables for comparisons\nInvented line the model added")
    updated = await memory.reconcile_related(object(), user, agent, "voice", "Give detailed answers with examples")
    assert updated == ["prefs"]
    assert blocks["prefs"] == "Use Markdown tables for comparisons"  # the invented line was not let in


@pytest.mark.asyncio
async def test_reconcile_leaves_blocks_alone_when_the_model_is_unclear_or_down(monkeypatch):
    user = _user()
    agent = _agent(user)
    blocks = {"prefs": "Short answers"}

    async def read(_db, _user, label, _agent=None):
        return blocks.get(label, "")

    async def upsert(*_a, **_k):
        raise AssertionError("nothing should be written")

    monkeypatch.setattr(memory, "read_core", read)
    monkeypatch.setattr(memory, "upsert_core", upsert)
    _llm_reply(monkeypatch, "Something unrelated entirely")
    assert await memory.reconcile_related(object(), user, agent, "voice", "Detailed answers") == []
    _llm_reply(monkeypatch, error=RuntimeError("down"))
    assert await memory.reconcile_related(object(), user, agent, "voice", "Detailed answers") == []
    _llm_reply(monkeypatch, "NONE")
    blocks["prefs"] = "Short answers"
    written = {}

    async def record(_db, _uid, label, value, _aid):
        written[label] = value

    monkeypatch.setattr(memory, "upsert_core", record)
    assert await memory.reconcile_related(object(), user, agent, "voice", "Detailed answers") == ["prefs"]
    assert written == {"prefs": ""}


@pytest.mark.asyncio
async def test_extractor_is_told_what_remember_for_me_already_saved(monkeypatch):
    seen = {}

    async def fake_complete(messages, **_kwargs):
        seen["prompt"] = messages[0]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])

    monkeypatch.setattr(memory_extract, "chat_complete", fake_complete)
    await memory_extract._extract_json("I moved", "Noted", [(4, "Lives in Bangalore")], ["Lives in Pune now"])
    assert "- Lives in Pune now" in seen["prompt"]
    assert "do not repeat" in seen["prompt"]
    assert "retire" in seen["prompt"]


@pytest.mark.asyncio
async def test_apply_extract_retires_a_known_fact_without_a_new_copy(monkeypatch):
    user, agent = _user(), _agent(_user())
    retired = []

    async def fake_retire(_db, _user, _agent, fid):
        retired.append(fid)
        return True

    async def no_remember(*_a, **_k):
        raise AssertionError("nothing new to store")

    monkeypatch.setattr(memory_extract, "retire_fact", fake_retire)
    monkeypatch.setattr(memory_extract, "remember", no_remember)
    known = [(4, "Lives in Bangalore"), (5, "Works at Acme")]
    out = await memory_extract.apply_extract(None, user, agent, {"retire": [4, 404, "4"]}, known=known)
    assert retired == [4]  # 404 was never shown; the repeat of 4 is ignored
    assert out["superseded"] == 1 and out["applied"] is True


@pytest.mark.asyncio
async def test_retire_fact_only_touches_facts_this_agent_can_see(monkeypatch):
    user = _user()
    mine, theirs = _agent(user), _agent(user)
    own = _fact(4, user, mine, "Lives in Bangalore")
    other = _fact(5, user, theirs, "Private to sales")
    forgotten = []
    _quiet_remember(monkeypatch, mine, forgotten)
    db = _RememberDB(user, [own, other])
    assert await memory.retire_fact(db, user, mine, 4) is True
    assert own.status == STATUS_SUPERSEDED and forgotten == ["Lives in Bangalore"]
    assert await memory.retire_fact(db, user, mine, 5) is False
    assert other.status == STATUS_ACTIVE


@pytest.mark.asyncio
async def test_rewrite_prompts_keep_the_parts_of_a_note_that_still_hold(monkeypatch):
    """A note that is only partly out of date (new city, same job) must not be dropped whole."""
    prompts = []

    async def fake(messages, **_kwargs):
        prompts.append(messages[0]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Lives in Pune\nWorks at Acme"))])

    monkeypatch.setattr("app.llm.chat_complete", fake)
    await memory.merge_block("learned", "Lives in Bangalore and works at Acme", "Lives in Pune now")
    await memory._drop_contradicted("prefs", "Lives in Bangalore and works at Acme", "Lives in Pune now")
    assert "partly out of date" in prompts[0] and "not the job" in prompts[0]
    assert "only partly out of date" in prompts[1]
