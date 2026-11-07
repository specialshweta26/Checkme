"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AppShell } from "../components/AppShell";
import { apiError, getMe, type Me } from "../lib";

// What I know: the user's own memory, shared by all their agents (apps/api/app/memory_engine/routes.py). People
// learned from mail, calendar and chat, facts about the user's world, and what changed this week, with edit, undo
// and forget. Nothing here is shared with other users.

type Person = {
  id: number;
  name: string;
  org: string;
  emails: string[];
  card: string;
  mail_in: number;
  mail_out: number;
  meetings: number;
  last_contact: string;
  facts: number;
  is_me: boolean;
};
type Fact = {
  id: number;
  text: string;
  status: string;
  trust: string;
  confidence: number;
  valid_from: string;
  valid_to: string;
  about?: string;
};
type PersonDetail = Person & { current: Fact[]; history: Fact[]; meets_with: { name: string; count: number }[] };
type Change = {
  id: number;
  op: string;
  label: string;
  before: string;
  after: string;
  reason: string;
  at: string;
  undone: boolean;
  can_undo: boolean;
};
type Tab = "week" | "people" | "facts";

function day(iso: string) {
  return iso ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "";
}

function ago(iso: string) {
  if (!iso) return "no contact yet";
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  return days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
}

function trustLabel(f: Fact) {
  return f.trust === "user_said" ? "you said" : f.trust === "third_party" ? "from someone else" : "guessed";
}

async function call(path: string, init?: RequestInit) {
  const res = await fetch(path, { credentials: "include", headers: { "Content-Type": "application/json" }, ...init });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(apiError(data, "Something went wrong."));
  return data;
}

export default function MemoryPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [tab, setTab] = useState<Tab>("week");
  const [people, setPeople] = useState<Person[]>([]);
  const [mine, setMine] = useState<Person[]>([]);
  const [facts, setFacts] = useState<Fact[]>([]);
  const [changes, setChanges] = useState<Change[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [open, setOpen] = useState<PersonDetail | null>(null);
  const [editing, setEditing] = useState<{ id: number; text: string } | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
    });
  }, []);

  const load = useCallback(async () => {
    try {
      const [p, f, c] = await Promise.all([call("/api/memory/people"), call("/api/memory/facts"), call("/api/memory/changes?days=7")]);
      setPeople(p.people);
      setMine(p.me);
      setFacts(f.facts);
      setChanges(c.changes);
      setCounts(c.counts);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    if (me) load();
  }, [me, load]);

  async function act(key: string, fn: () => Promise<unknown>) {
    setBusy(key);
    try {
      await fn();
      await load();
      if (open) setOpen(await call(`/api/memory/people/${open.id}`).catch(() => null));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  const q = query.trim().toLowerCase();
  const shownPeople = useMemo(
    () => people.filter((p) => !q || p.name.toLowerCase().includes(q) || p.org.toLowerCase().includes(q) || p.emails.some((e) => e.includes(q))),
    [people, q],
  );
  const shownFacts = useMemo(() => facts.filter((f) => !q || f.text.toLowerCase().includes(q) || (f.about || "").toLowerCase().includes(q)), [facts, q]);
  const learned = (counts.add || 0) + (counts.card || 0);
  const tidied = (counts.supersede || 0) + (counts.fade || 0) + (counts.merge || 0);

  function factRow(f: Fact, withAbout = true) {
    const isEditing = editing?.id === f.id;
    return (
      <div key={f.id} className="task-card">
        <div className="task-main">
          {isEditing ? (
            <textarea className="field" rows={2} value={editing.text} onChange={(e) => setEditing({ id: f.id, text: e.target.value })} />
          ) : (
            <strong>{f.text}</strong>
          )}
          <div className="task-meta">
            {withAbout && f.about ? <span className="app-pill">{f.about}</span> : null}
            <span className="muted">{trustLabel(f)}</span>
            <span className="faint">since {day(f.valid_from)}</span>
            {f.status === "active" ? <span className="faint">confidence {Math.round(f.confidence * 100)}%</span> : <span className="faint">until {day(f.valid_to)}</span>}
          </div>
        </div>
        {f.status === "active" ? (
          <div className="task-actions">
            {isEditing ? (
              <>
                <button className="btn" disabled={!!busy || !editing.text.trim()} onClick={() => act(`edit${f.id}`, async () => {
                  await call(`/api/memory/facts/${f.id}`, { method: "PATCH", body: JSON.stringify({ text: editing.text }) });
                  setEditing(null);
                })}>Save</button>
                <button className="btn secondary" onClick={() => setEditing(null)}>Cancel</button>
              </>
            ) : (
              <>
                <button className="icon-btn" title="Correct this fact" disabled={!!busy} onClick={() => setEditing({ id: f.id, text: f.text })}>✎</button>
                <button className="icon-btn" title="Delete this fact" disabled={!!busy} onClick={() => act(`del${f.id}`, () => call(`/api/memory/facts/${f.id}`, { method: "DELETE" }))}>×</button>
              </>
            )}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <AppShell me={me} current="memory">
      <div className="page">
        <div className="tasks-page">
          <div className="page-hero">
            <div>
              <h1>What I know</h1>
              <p className="muted">
                What your assistant has learned from your chats, mail and calendar. Every one of your agents uses it; nobody else can see it.
                Correct anything that's wrong, undo a change, or forget a person everywhere.
              </p>
            </div>
          </div>

          <div className="tasks-toolbar">
            <div className="tasks-tabs">
              {(["week", "people", "facts"] as Tab[]).map((id) => (
                <button key={id} className={`pill${tab === id ? " on" : ""}`} onClick={() => setTab(id)}>
                  {id === "week" ? "This week" : id === "people" ? `People (${people.length})` : `Facts (${facts.length})`}
                </button>
              ))}
            </div>
            <div className="tasks-filters">
              <input className="tasks-search" type="search" placeholder="Search memory" value={query} onChange={(e) => setQuery(e.target.value)} />
            </div>
          </div>

          {error ? <p className="err">{error}</p> : null}

          {tab === "week" ? (
            <div className="task-list">
              <p className="muted">
                {loaded ? `In the last 7 days: ${learned} learned, ${tidied} tidied up (replaced, faded or merged).` : "Loading…"}
              </p>
              {loaded && changes.length === 0 ? (
                <div className="task-empty">
                  <p className="muted">Nothing changed this week.</p>
                </div>
              ) : null}
              {changes
                .filter((c) => !q || `${c.before} ${c.after} ${c.reason}`.toLowerCase().includes(q))
                .map((c) => (
                  <div key={c.id} className="task-card">
                    <div className="task-main">
                      <strong>
                        {c.label}
                        {c.undone ? " (undone)" : ""}
                      </strong>
                      {c.op === "supersede" ? (
                        <span className="muted">
                          “{c.before}” → “{c.after}”
                        </span>
                      ) : (
                        <span className="muted">{c.after || c.before}</span>
                      )}
                      <div className="task-meta">
                        <span className="faint">{c.reason}</span>
                        <span className="faint">{day(c.at)}</span>
                      </div>
                    </div>
                    {c.can_undo ? (
                      <div className="task-actions">
                        <button className="btn secondary" disabled={!!busy} onClick={() => act(`undo${c.id}`, () => call(`/api/memory/changes/${c.id}/undo`, { method: "POST" }))}>
                          Undo
                        </button>
                      </div>
                    ) : null}
                  </div>
                ))}
            </div>
          ) : null}

          {tab === "people" ? (
            <div className="task-list">
              {loaded && shownPeople.length === 0 ? (
                <div className="task-empty">
                  <p className="muted">No people yet. They appear as your assistant reads your mail and calendar, or as you mention them in chat.</p>
                </div>
              ) : null}
              {shownPeople.map((p) => (
                <div key={p.id} className="task-card">
                  <button className="task-main ghost" onClick={() => (open?.id === p.id ? setOpen(null) : call(`/api/memory/people/${p.id}`).then(setOpen).catch((e) => setError(e.message)))}>
                    <strong>{p.name}</strong>
                    {p.card ? <span className="muted">{p.card}</span> : null}
                    <div className="task-meta">
                      {p.org ? <span className="app-pill">{p.org}</span> : null}
                      <span className="muted">
                        {p.mail_out} sent · {p.mail_in} received · {p.meetings} meetings
                      </span>
                      <span className="faint">last contact {ago(p.last_contact)}</span>
                      {p.facts ? <span className="faint">{p.facts} facts</span> : null}
                    </div>
                  </button>
                  {open?.id === p.id ? (
                    <div className="task-detail stack">
                      {open.emails.length ? <p className="faint">{open.emails.join(", ")}</p> : null}
                      {open.meets_with.length ? <p className="muted">Often meets with {open.meets_with.map((m) => m.name).join(", ")}</p> : null}
                      {open.current.map((f) => factRow(f, false))}
                      {open.history.length ? (
                        <details>
                          <summary className="muted">Earlier ({open.history.length})</summary>
                          {open.history.map((f) => factRow(f, false))}
                        </details>
                      ) : null}
                      <div className="row">
                        <button className="btn secondary" disabled={!!busy} onClick={() => act(`me${p.id}`, () => call(`/api/memory/people/${p.id}/me`, { method: "POST", body: JSON.stringify({ is_me: true }) }))}>
                          This is me
                        </button>
                        <button className="btn secondary" disabled={!!busy} onClick={() => {
                          if (window.confirm(`Forget ${p.name} everywhere? Their record and every fact about them are removed.`)) {
                            act(`forget${p.id}`, async () => {
                              await call(`/api/memory/people/${p.id}`, { method: "DELETE" });
                              setOpen(null);
                            });
                          }
                        }}>
                          Forget {p.name}
                        </button>
                      </div>
                    </div>
                  ) : null}
                </div>
              ))}
              {mine.length ? (
                <details>
                  <summary className="muted">Your own other addresses ({mine.length}) — not counted as contacts</summary>
                  {mine.map((p) => (
                    <div key={p.id} className="task-card">
                      <div className="task-main">
                        <strong>{p.name}</strong>
                        <span className="faint">{p.emails.join(", ")}</span>
                      </div>
                      <div className="task-actions">
                        <button className="btn secondary" disabled={!!busy} onClick={() => act(`notme${p.id}`, () => call(`/api/memory/people/${p.id}/me`, { method: "POST", body: JSON.stringify({ is_me: false }) }))}>
                          Not me
                        </button>
                      </div>
                    </div>
                  ))}
                </details>
              ) : null}
            </div>
          ) : null}

          {tab === "facts" ? (
            <div className="task-list">
              {loaded && shownFacts.length === 0 ? (
                <div className="task-empty">
                  <p className="muted">No facts yet. Tell any of your agents something to remember, and it shows up here.</p>
                </div>
              ) : null}
              {shownFacts.map((f) => factRow(f))}
            </div>
          ) : null}
        </div>
      </div>
    </AppShell>
  );
}
