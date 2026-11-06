"use client";

import { useEffect, useMemo, useState } from "react";
import { AppShell } from "../components/AppShell";
import { apiError, getMe, type Me } from "../lib";

// Saved browser runs (docs/BROWSING.md "Healing saved runs"): a finished browser task or a taught skill's
// recording, replayed without a model call per step. Exact steps replay by selector; AI steps are sentences the
// model carries out each run. A fix found when a site changed waits here for approval.

type StepRow = { index: number; text: string; ai: boolean };
type Change = { step: number; was: string; now: string[]; error?: string };
type SavedRun = {
  id: string;
  name: string;
  goal: string;
  host: string;
  start_url: string;
  source: string;
  params: { name: string; secret: boolean }[];
  steps: string[];
  step_rows: StepRow[];
  uses: number;
  successes: number;
  failures: number;
  proposed_fix: { steps: string[]; changes: Change[]; at: string | null } | null;
  can_undo: boolean;
  updated_at: string | null;
};

type SavedLogin = { id: string; host: string; label: string; username_hint: string; last_used_at: string | null };

// One row of the editor: an exact saved step (kept by its index) or an AI step (a sentence).
type Draft = { key: string; keep?: number; goal?: string; text: string };

export default function SavedRunsPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [runs, setRuns] = useState<SavedRun[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState("");
  const [editing, setEditing] = useState("");
  const [draft, setDraft] = useState<Draft[]>([]);
  const [draftName, setDraftName] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [logins, setLogins] = useState<SavedLogin[]>([]);
  const [loginForm, setLoginForm] = useState({ host: "", username: "", password: "", label: "" });
  const [addingLogin, setAddingLogin] = useState(false);

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
    });
  }, []);

  async function refresh() {
    const res = await fetch("/api/browser/saved-runs", { credentials: "include" });
    if (res.status === 401) {
      window.location.replace("/login");
      return;
    }
    const data = await res.json().catch(() => []);
    if (!res.ok) {
      setError(apiError(data, "Could not load saved runs"));
      return;
    }
    setRuns(Array.isArray(data) ? data : []);
    setLoaded(true);
    const saved = await fetch("/api/browser/logins", { credentials: "include" });
    const rows = await saved.json().catch(() => []);
    if (saved.ok && Array.isArray(rows)) setLogins(rows);
  }

  async function addLogin() {
    setBusy("login");
    setError("");
    try {
      const res = await fetch("/api/browser/logins", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(loginForm),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(apiError(data, "Could not save the sign-in"));
      setLoginForm({ host: "", username: "", password: "", label: "" });
      setAddingLogin(false);
      await refresh();
    } catch (err: any) {
      setError(err.message || "Could not save the sign-in");
    } finally {
      setBusy("");
    }
  }

  async function removeLogin(row: SavedLogin) {
    if (!window.confirm(`Remove the saved sign-in for ${row.host}?`)) return;
    setBusy("login");
    const res = await fetch(`/api/browser/logins/${row.id}`, { method: "DELETE", credentials: "include" });
    if (!res.ok) setError("Could not remove the sign-in");
    setBusy("");
    await refresh();
  }

  useEffect(() => {
    if (me) refresh();
  }, [me]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = [...runs].sort((a, b) => Number(!!b.proposed_fix) - Number(!!a.proposed_fix));
    if (!q) return rows;
    return rows.filter((r) => `${r.name} ${r.goal} ${r.host} ${r.steps.join(" ")}`.toLowerCase().includes(q));
  }, [runs, query]);

  async function call(id: string, path: string, method: string, body?: unknown) {
    setBusy(id + path);
    setError("");
    try {
      const res = await fetch(`/api/browser/saved-runs/${id}${path}`, {
        method,
        credentials: "include",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(apiError(data, "That did not work"));
      await refresh();
      return true;
    } catch (err: any) {
      setError(err.message || "That did not work");
      return false;
    } finally {
      setBusy("");
    }
  }

  function startEdit(run: SavedRun) {
    setEditing(run.id);
    setExpanded(run.id);
    setDraftName(run.name);
    setDraft(
      run.step_rows.map((row) =>
        row.ai ? { key: `g${row.index}`, goal: row.text, text: row.text } : { key: `k${row.index}`, keep: row.index, text: row.text },
      ),
    );
  }

  function move(i: number, by: number) {
    setDraft((rows) => {
      const j = i + by;
      if (j < 0 || j >= rows.length) return rows;
      const next = [...rows];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  }

  async function saveEdit(run: SavedRun) {
    const steps = draft.map((row) => (row.goal !== undefined ? { goal: row.goal } : { keep: row.keep }));
    const ok = await call(run.id, "/steps", "PUT", { steps, name: draftName });
    if (ok) setEditing("");
  }

  return (
    <AppShell me={me} current="runs">
      <div className="page">
        <div className="tasks-page">
          <div className="page-hero">
            <div>
              <h1>Saved runs</h1>
              <p className="muted">
                Browser work the assistant learned, replayed step by step without guessing. Exact steps repeat the same clicks; AI steps
                are sentences it works out on the page each time. When a site changes, the run fixes itself and the fix waits here for
                you.
              </p>
            </div>
          </div>

          <div className="tasks-toolbar">
            <div className="tasks-tabs">
              <span className="muted">
                {runs.length} saved{runs.some((r) => r.proposed_fix) ? ` · ${runs.filter((r) => r.proposed_fix).length} fix to review` : ""}
              </span>
            </div>
            <div className="tasks-filters">
              <input className="tasks-search" type="search" placeholder="Search saved runs" value={query} onChange={(e) => setQuery(e.target.value)} />
            </div>
          </div>

          {error ? <p className="err">{error}</p> : null}

          <div className="task-list">
            {loaded && visible.length === 0 ? (
              <div className="task-empty">
                <p className="muted">
                  {runs.length === 0
                    ? "Nothing saved yet. When the assistant finishes a browser task, or you teach it a skill, the steps are saved here."
                    : "No saved runs match."}
                </p>
              </div>
            ) : null}
            {visible.map((run) => {
              const open = expanded === run.id;
              const isEditing = editing === run.id;
              return (
                <div key={run.id} className="task-card">
                  <button className="task-main ghost" onClick={() => setExpanded(open ? "" : run.id)}>
                    <strong>{run.name || run.goal}</strong>
                    <span className="muted">{run.goal}</span>
                    <div className="task-meta">
                      {run.proposed_fix ? <span className="status-dot paused">Fix to review</span> : null}
                      <span className="app-pill">{run.host}</span>
                      <span className="muted">
                        {run.steps.length} step{run.steps.length === 1 ? "" : "s"}
                      </span>
                      <span className="faint">
                        {run.successes}/{run.uses} clean run{run.uses === 1 ? "" : "s"}
                      </span>
                      {run.source === "teach" ? <span className="faint">taught</span> : null}
                    </div>
                  </button>
                  <div className="task-actions">
                    {!isEditing ? (
                      <button className="icon-btn" disabled={!!busy} onClick={() => startEdit(run)}>
                        Edit
                      </button>
                    ) : null}
                    {run.can_undo ? (
                      <button className="icon-btn" title="Put back the steps from before the last change" disabled={!!busy} onClick={() => call(run.id, "/undo", "POST")}>
                        Undo
                      </button>
                    ) : null}
                    <button
                      className="icon-btn"
                      disabled={!!busy}
                      onClick={() => {
                        if (window.confirm(`Delete the saved run "${run.name || run.goal}"?`)) call(run.id, "", "DELETE");
                      }}
                    >
                      Delete
                    </button>
                  </div>

                  {run.proposed_fix && !isEditing ? (
                    <div className="run-fix">
                      <strong>The site changed, and this run fixed itself.</strong>
                      <ul>
                        {run.proposed_fix.changes.map((c) => (
                          <li key={c.step}>
                            Step {c.step}: <s>{c.was}</s> → {c.now.join(", then ")}
                          </li>
                        ))}
                      </ul>
                      <p className="muted">Nothing changes until you keep it. Until then, runs use the old steps and fix themselves again.</p>
                      <div className="row">
                        <button className="btn" disabled={!!busy} onClick={() => call(run.id, "/fix", "POST", { accept: true })}>
                          Keep the fix
                        </button>
                        <button className="btn secondary" disabled={!!busy} onClick={() => call(run.id, "/fix", "POST", { accept: false })}>
                          Discard
                        </button>
                      </div>
                    </div>
                  ) : null}

                  {open && !isEditing ? (
                    <div className="task-detail">
                      <ol className="run-steps">
                        {run.step_rows.map((row) => (
                          <li key={row.index}>
                            {row.ai ? <span className="run-ai">AI</span> : null}
                            {row.text}
                          </li>
                        ))}
                      </ol>
                      {run.params.length ? (
                        <p className="muted" style={{ fontSize: 12 }}>
                          Asks for: {run.params.map((p) => (p.secret ? `${p.name} (secret)` : p.name)).join(", ")}
                        </p>
                      ) : null}
                      {run.start_url ? (
                        <p className="faint" style={{ fontSize: 12 }}>
                          Starts at {run.start_url}
                        </p>
                      ) : null}
                    </div>
                  ) : null}

                  {isEditing ? (
                    <div className="task-detail stack">
                      <label className="stack">
                        <span className="muted">Name</span>
                        <input className="field" value={draftName} onChange={(e) => setDraftName(e.target.value)} />
                      </label>
                      <p className="muted" style={{ fontSize: 12 }}>
                        Rewrite a step in your own words to make it an AI step, for example “Set the date range to last month”. Use {"{{name}}"} for a
                        value you give each run.
                      </p>
                      <ol className="run-steps editing">
                        {draft.map((row, i) => (
                          <li key={row.key}>
                            {row.goal !== undefined ? (
                              <textarea
                                className="field"
                                rows={2}
                                value={row.goal}
                                placeholder="What should this step do?"
                                onChange={(e) => setDraft((rows) => rows.map((r, j) => (j === i ? { ...r, goal: e.target.value } : r)))}
                              />
                            ) : (
                              <span className="run-exact">{row.text}</span>
                            )}
                            <span className="run-step-tools">
                              {row.goal === undefined ? (
                                <button type="button" className="icon-btn" onClick={() => setDraft((rows) => rows.map((r, j) => (j === i ? { key: r.key, goal: r.text, text: r.text } : r)))}>
                                  Rewrite
                                </button>
                              ) : null}
                              <button type="button" className="icon-btn" aria-label="Move up" disabled={i === 0} onClick={() => move(i, -1)}>
                                ↑
                              </button>
                              <button type="button" className="icon-btn" aria-label="Move down" disabled={i === draft.length - 1} onClick={() => move(i, 1)}>
                                ↓
                              </button>
                              <button type="button" className="icon-btn" aria-label="Remove step" onClick={() => setDraft((rows) => rows.filter((_, j) => j !== i))}>
                                ×
                              </button>
                            </span>
                          </li>
                        ))}
                      </ol>
                      <div className="row">
                        <button
                          type="button"
                          className="btn secondary"
                          onClick={() => setDraft((rows) => [...rows, { key: `n${Date.now()}`, goal: "", text: "" }])}
                        >
                          Add AI step
                        </button>
                        <span style={{ flex: 1 }} />
                        <button type="button" className="btn secondary" onClick={() => setEditing("")}>
                          Cancel
                        </button>
                        <button
                          type="button"
                          className="btn"
                          disabled={!!busy || draft.length === 0 || draft.some((r) => r.goal !== undefined && !r.goal.trim())}
                          onClick={() => saveEdit(run)}
                        >
                          {busy === run.id + "/steps" ? "Saving…" : "Save steps"}
                        </button>
                      </div>
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>

          <div className="page-hero" style={{ marginTop: 28 }}>
            <div>
              <h2>Saved sign-ins</h2>
              <p className="muted">
                For sites the assistant signs in to on its own (scheduled runs, saved runs). Stored encrypted. The assistant types them into the
                site but never sees them, and you never type a password in chat. Codes sent to your phone or email, and CAPTCHAs, still come
                to you.
              </p>
            </div>
            {!addingLogin ? (
              <button className="btn" onClick={() => setAddingLogin(true)}>
                Add sign-in
              </button>
            ) : null}
          </div>
          {addingLogin ? (
            <form
              className="task-card login-form"
              autoComplete="off"
              onSubmit={(e) => {
                e.preventDefault();
                addLogin();
              }}
            >
              <input className="field" required placeholder="Site, for example portal.example.com" value={loginForm.host} onChange={(e) => setLoginForm({ ...loginForm, host: e.target.value })} />
              <input className="field" placeholder="Name (optional)" value={loginForm.label} onChange={(e) => setLoginForm({ ...loginForm, label: e.target.value })} />
              <input className="field" required autoComplete="off" placeholder="Username or email" value={loginForm.username} onChange={(e) => setLoginForm({ ...loginForm, username: e.target.value })} />
              <input
                className="field"
                type="password"
                autoComplete="new-password"
                placeholder="Password (leave empty to keep the saved one)"
                value={loginForm.password}
                onChange={(e) => setLoginForm({ ...loginForm, password: e.target.value })}
              />
              <div className="row">
                <span style={{ flex: 1 }} />
                <button type="button" className="btn secondary" onClick={() => setAddingLogin(false)}>
                  Cancel
                </button>
                <button className="btn" disabled={busy === "login" || !loginForm.host.trim() || !loginForm.username.trim()}>
                  {busy === "login" ? "Saving…" : "Save sign-in"}
                </button>
              </div>
            </form>
          ) : null}
          <div className="task-list">
            {logins.length === 0 && !addingLogin ? <p className="muted">No saved sign-ins.</p> : null}
            {logins.map((row) => (
              <div key={row.id} className="task-card">
                <div className="task-main">
                  <strong>{row.label}</strong>
                  <div className="task-meta">
                    <span className="app-pill">{row.host}</span>
                    <span className="muted">{row.username_hint}</span>
                    <span className="faint">{row.last_used_at ? `last used ${new Date(row.last_used_at).toLocaleDateString()}` : "not used yet"}</span>
                  </div>
                </div>
                <div className="task-actions">
                  <button
                    className="icon-btn"
                    title="Change the username or password"
                    disabled={!!busy}
                    onClick={() => {
                      setLoginForm({ host: row.host, username: "", password: "", label: row.label === row.host ? "" : row.label });
                      setAddingLogin(true);
                    }}
                  >
                    Change
                  </button>
                  <button className="icon-btn" disabled={!!busy} onClick={() => removeLogin(row)}>
                    Remove
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
