"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AppShell } from "../components/AppShell";
import { apiError, getMe, type Me } from "../lib";

// What I know: the user's own memory, shared by all their agents (apps/api/app/memory_engine/routes.py). People
// learned from mail, calendar and chat, facts about the user's world, and what changed this week. The user can add
// notes, correct or remove anything, and undo changes. Nothing here is shared with other users.

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
  source: string;
  unsure: boolean;
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
  undo_label: string;
};
type Reconnect = { id: number; name: string; days_quiet: number; mail_out: number; meetings: number };
type Tab = "people" | "facts" | "week";

function day(iso: string) {
  return iso ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "";
}

function ago(iso: string) {
  if (!iso) return "no contact yet";
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  return days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
}

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function firstName(name: string) {
  return name.split(/\s+/)[0] || name;
}

function contactLine(p: Person) {
  const bits = [];
  if (p.mail_out) bits.push(`you emailed ${p.mail_out}×`);
  if (p.mail_in) bits.push(`emailed you ${p.mail_in}×`);
  if (p.meetings) bits.push(plural(p.meetings, "meeting"));
  bits.push(`last contact ${ago(p.last_contact)}`);
  return bits.join(" · ");
}

async function call(path: string, init?: RequestInit) {
  const res = await fetch(path, { credentials: "include", headers: { "Content-Type": "application/json" }, ...init });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(apiError(data, "Something went wrong."));
  return data;
}

export default function MemoryPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [tab, setTab] = useState<Tab>("people");
  const [people, setPeople] = useState<Person[]>([]);
  const [mine, setMine] = useState<Person[]>([]);
  const [facts, setFacts] = useState<Fact[]>([]);
  const [changes, setChanges] = useState<Change[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [quiet, setQuiet] = useState<Reconnect[]>([]);
  const [open, setOpen] = useState<PersonDetail | null>(null);
  const [editing, setEditing] = useState<{ id: number; text: string } | null>(null);
  const [note, setNote] = useState("");
  const [newFact, setNewFact] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
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
      const [p, f, c, s] = await Promise.all([
        call("/api/memory/people"),
        call("/api/memory/facts"),
        call("/api/memory/changes?days=7"),
        call("/api/memory/suggestions").catch(() => ({ reconnect: [] })),
      ]);
      setPeople(p.people);
      setMine(p.me);
      setFacts(f.facts);
      setChanges(c.changes);
      setCounts(c.counts);
      setQuiet(s.reconnect || []);
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

  async function act(key: string, fn: () => Promise<unknown>, done = "") {
    setBusy(key);
    setNotice("");
    try {
      await fn();
      await load();
      if (open) setOpen(await call(`/api/memory/people/${open.id}`).catch(() => null));
      if (done) setNotice(done);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function openPerson(p: Person) {
    if (open?.id === p.id) {
      setOpen(null);
      return;
    }
    setNote("");
    setEditing(null);
    try {
      setOpen(await call(`/api/memory/people/${p.id}`));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const q = query.trim().toLowerCase();
  const shownPeople = useMemo(
    () => people.filter((p) => !q || p.name.toLowerCase().includes(q) || p.org.toLowerCase().includes(q) || p.emails.some((e) => e.includes(q))),
    [people, q],
  );
  const shownFacts = useMemo(() => facts.filter((f) => !q || f.text.toLowerCase().includes(q) || (f.about || "").toLowerCase().includes(q)), [facts, q]);
  const learned = counts.add || 0;
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
            <span className="muted">{f.source}</span>
            {f.unsure ? <span className="status-dot paused">Not sure</span> : null}
            {f.status === "active" ? <span className="faint">since {day(f.valid_from)}</span> : <span className="faint">until {day(f.valid_to)}</span>}
          </div>
        </div>
        {f.status === "active" ? (
          <div className="task-actions">
            {isEditing ? (
              <>
                <button
                  className="btn"
                  disabled={!!busy || !editing.text.trim()}
                  onClick={() =>
                    act(`edit${f.id}`, async () => {
                      await call(`/api/memory/facts/${f.id}`, { method: "PATCH", body: JSON.stringify({ text: editing.text }) });
                      setEditing(null);
                    }, "Saved. The old version is kept under Earlier.")
                  }
                >
                  Save
                </button>
                <button className="btn secondary" onClick={() => setEditing(null)}>
                  Cancel
                </button>
              </>
            ) : (
              <>
                <button className="btn secondary" disabled={!!busy} onClick={() => setEditing({ id: f.id, text: f.text })}>
                  Correct
                </button>
                <button className="btn secondary" disabled={!!busy} onClick={() => act(`del${f.id}`, () => call(`/api/memory/facts/${f.id}`, { method: "DELETE" }), "Removed.")}>
                  Remove
                </button>
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
                What your assistant uses when it drafts replies, summarises mail and prepares you for meetings. Add what it should know, correct
                what's wrong, or remove anything. Only you can see this.
              </p>
            </div>
          </div>

          <div className="tasks-toolbar">
            <div className="tasks-tabs">
              {(["people", "facts", "week"] as Tab[]).map((id) => (
                <button key={id} className={`pill${tab === id ? " on" : ""}`} onClick={() => setTab(id)}>
                  {id === "people" ? `People (${people.length})` : id === "facts" ? `Facts (${facts.length})` : "This week"}
                </button>
              ))}
            </div>
            <div className="tasks-filters">
              <input className="tasks-search" type="search" placeholder="Search" value={query} onChange={(e) => setQuery(e.target.value)} />
            </div>
          </div>

          {error ? <p className="err">{error}</p> : null}
          {notice ? <p className="att-summary">{notice}</p> : null}

          {tab === "people" ? (
            <div className="task-list">
              {loaded && shownPeople.length === 0 ? (
                <div className="task-empty">
                  <p className="muted">No people yet. They appear as your assistant reads your mail and calendar, or as you mention them in chat.</p>
                </div>
              ) : null}
              {shownPeople.map((p) => (
                <div key={p.id} className="task-card">
                  <button className="task-main ghost" onClick={() => openPerson(p)}>
                    <strong>{p.name}</strong>
                    <span className="muted">{p.card || "Nothing noted yet."}</span>
                    <div className="task-meta">
                      {p.org ? <span className="app-pill">{p.org}</span> : null}
                      <span className="faint">{contactLine(p)}</span>
                    </div>
                  </button>
                  {open?.id === p.id ? (
                    <div className="task-detail stack">
                      {open.emails.length ? <p className="faint">{open.emails.join(", ")}</p> : null}

                      <div className="stack">
                        <strong>Add a note about {firstName(p.name)}</strong>
                        <textarea
                          className="field"
                          rows={2}
                          placeholder={`For example: Head of Retail Ops; prefers a call over email; we're working on the Q1 rollout`}
                          value={note}
                          onChange={(e) => setNote(e.target.value)}
                        />
                        <div className="row">
                          <button
                            className="btn"
                            disabled={!!busy || note.trim().length < 2}
                            onClick={() =>
                              act(`note${p.id}`, async () => {
                                await call(`/api/memory/people/${p.id}/notes`, { method: "POST", body: JSON.stringify({ text: note }) });
                                setNote("");
                              }, `Saved. Your assistant will use this whenever ${firstName(p.name)} comes up.`)
                            }
                          >
                            {busy === `note${p.id}` ? "Saving…" : "Save note"}
                          </button>
                          <span className="faint">The summary above is rewritten from your notes and what it has learned.</span>
                        </div>
                      </div>

                      <strong>What it knows ({open.current.length})</strong>
                      {open.current.length ? open.current.map((f) => factRow(f, false)) : <p className="muted">Nothing yet. Add a note above.</p>}
                      {open.history.length ? (
                        <details>
                          <summary className="muted">Earlier, no longer true ({open.history.length})</summary>
                          {open.history.map((f) => factRow(f, false))}
                        </details>
                      ) : null}
                      {open.meets_with.length ? <p className="muted">Often in meetings with {open.meets_with.slice(0, 5).map((m) => m.name).join(", ")}.</p> : null}

                      <details>
                        <summary className="muted">Something wrong with this entry?</summary>
                        <div className="row">
                          <button
                            className="btn secondary"
                            disabled={!!busy}
                            title="Use this if the address is yours (for example your personal email). It stops being counted as a contact."
                            onClick={() =>
                              act(`me${p.id}`, async () => {
                                await call(`/api/memory/people/${p.id}/me`, { method: "POST", body: JSON.stringify({ is_me: true }) });
                                setOpen(null);
                              }, `Got it: ${p.emails[0] || p.name} is one of your own addresses.`)
                            }
                          >
                            This is one of my own addresses
                          </button>
                          <button
                            className="btn secondary"
                            disabled={!!busy}
                            onClick={() => {
                              if (window.confirm(`Remove ${p.name} from memory? Everything your assistant knows about them is deleted.`)) {
                                act(`forget${p.id}`, async () => {
                                  await call(`/api/memory/people/${p.id}`, { method: "DELETE" });
                                  setOpen(null);
                                }, `${p.name} was removed from memory.`);
                              }
                            }}
                          >
                            Remove {firstName(p.name)} from memory
                          </button>
                        </div>
                      </details>
                    </div>
                  ) : null}
                </div>
              ))}
              {mine.length ? (
                <details>
                  <summary className="muted">Your own addresses ({mine.length}): not counted as contacts</summary>
                  {mine.map((p) => (
                    <div key={p.id} className="task-card">
                      <div className="task-main">
                        <strong>{p.emails.join(", ") || p.name}</strong>
                      </div>
                      <div className="task-actions">
                        <button
                          className="btn secondary"
                          disabled={!!busy}
                          onClick={() => act(`notme${p.id}`, () => call(`/api/memory/people/${p.id}/me`, { method: "POST", body: JSON.stringify({ is_me: false }) }), "Moved back to your contacts.")}
                        >
                          Not mine
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
              <div className="task-card">
                <div className="task-main">
                  <strong>Tell your assistant something to remember</strong>
                  <textarea
                    className="field"
                    rows={2}
                    placeholder="For example: I'm based in Bangalore; I prefer short bullet-point answers; my manager is Priya Nair"
                    value={newFact}
                    onChange={(e) => setNewFact(e.target.value)}
                  />
                </div>
                <div className="task-actions">
                  <button
                    className="btn"
                    disabled={!!busy || newFact.trim().length < 2}
                    onClick={() =>
                      act("newfact", async () => {
                        await call("/api/memory/facts", { method: "POST", body: JSON.stringify({ text: newFact }) });
                        setNewFact("");
                      }, "Saved. All your agents will use it.")
                    }
                  >
                    Save
                  </button>
                </div>
              </div>
              {loaded && shownFacts.length === 0 ? (
                <div className="task-empty">
                  <p className="muted">No facts yet.</p>
                </div>
              ) : null}
              {shownFacts.map((f) => factRow(f))}
            </div>
          ) : null}

          {tab === "week" ? (
            <div className="task-list">
              <p className="muted">
                {loaded ? `In the last 7 days your assistant learned ${plural(learned, "thing")} and tidied up ${tidied} (replaced, faded or merged).` : "Loading…"}
              </p>
              {quiet.length ? (
                <div className="task-card">
                  <div className="task-main">
                    <strong>Worth a quick note</strong>
                    <span className="muted">People you work with a lot who have gone quiet.</span>
                    {quiet.map((r) => (
                      <span key={r.id} className="muted">
                        {r.name}: no contact for {r.days_quiet} days
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}
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
                          “{c.after}” replaced “{c.before}”
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
                        <button className="btn secondary" disabled={!!busy} onClick={() => act(`undo${c.id}`, () => call(`/api/memory/changes/${c.id}/undo`, { method: "POST" }), "Done.")}>
                          {c.undo_label}
                        </button>
                      </div>
                    ) : null}
                  </div>
                ))}
            </div>
          ) : null}
        </div>
      </div>
    </AppShell>
  );
}
