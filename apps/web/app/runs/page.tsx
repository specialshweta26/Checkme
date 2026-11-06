"use client";

import { useEffect, useMemo, useState } from "react";
import { AppShell } from "../components/AppShell";
import { apiError, getMe, type Me } from "../lib";

// Saved browser runs (docs/BROWSING.md "Healing saved runs"): a finished browser task or a taught skill's
// recording, replayed without a model call per step. Exact steps replay by selector; AI steps are sentences the
// model carries out each run. A fix found when a site changed waits here for approval.

type StepRow = {
  index: number;
  text: string;
  ai: boolean;
  approve?: boolean;
  check?: { text: string; yes: number; no: number };
  variable?: string;
  label?: string;
};
type Change = { step: number; was: string; now: string[]; error?: string };
type SavedRun = {
  id: string;
  name: string;
  goal: string;
  host: string;
  start_url: string;
  source: string;
  params: { name: string; secret: boolean; label?: string; example?: string }[];
  steps: string[];
  step_rows: StepRow[];
  uses: number;
  successes: number;
  failures: number;
  proposed_fix: { steps: string[]; changes: Change[]; at: string | null } | null;
  can_undo: boolean;
  has_run_key?: boolean;
  updated_at: string | null;
};

type RunSummary = { id: string; goal: string; host: string; status: string; result: string; trace_id: string | null; steps: number; failed_steps: number; started_at: string | null };
type RunStep = { i: number; at: string; action: string; why: string; ok: boolean; url: string; title: string; error?: string; warnings?: string[]; shot_url: string | null };
type RunDetail = RunSummary & { step_rows: RunStep[] };

const RUN_STATUS: Record<string, string> = {
  completed: "Completed",
  replayed: "Replayed",
  recovered: "Fixed itself",
  stopped: "Stopped",
  needs_you: "Needs you",
  running: "Running",
};

type SavedLogin = { id: string; host: string; label: string; username_hint: string; last_used_at: string | null };

// One row of the editor: an exact saved step (kept by its index) or an AI step (a sentence).
type Draft = { key: string; keep?: number; goal?: string; approve?: string; check?: string; yes?: number; no?: number; text: string };

// The saved run as a graph: each step a node, arrows to the next one, and a check node branching to its yes / no
// steps (docs/BROWSING.md "Checks").
function FlowGraph({ rows }: { rows: StepRow[] }) {
  const goto = (n: number) => (n ? `step ${n}` : "end");
  return (
    <div className="flow">
      {rows.map((row, i) => {
        const kind = row.check ? "check" : row.approve ? "approve" : row.ai ? "ai" : row.variable ? "var" : "exact";
        const label = row.check ? row.check.text : row.text;
        return (
          <div key={row.index} className="flow-item">
            <div className={`flow-node ${kind}`}>
              <span className="flow-num">{i + 1}</span>
              <span className="flow-text">
                {kind === "ai" ? <span className="run-ai">AI</span> : null}
                {kind === "approve" ? <span className="run-ai run-ok">OK?</span> : null}
                {kind === "check" ? <span className="run-ai run-check">Check</span> : null}
                {label}
                {row.variable && !label.includes(`{{${row.variable}}}`) ? <code className="flow-var">{`{{${row.variable}}}`}</code> : null}
              </span>
            </div>
            {row.check ? (
              <div className="flow-branches">
                <span className="flow-branch yes">yes → {goto(row.check.yes)}</span>
                <span className="flow-branch no">no → {goto(row.check.no)}</span>
              </div>
            ) : i < rows.length - 1 ? (
              <div className="flow-arrow" aria-hidden />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

// "from_date,to_date\n2026-05-01,2026-05-07" -> [{from_date, to_date}]
function parseRows(csv: string): Record<string, string>[] {
  const lines = csv.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (lines.length < 2) return [];
  const head = lines[0].split(",").map((h) => h.trim());
  return lines.slice(1).map((line) => {
    const cells = line.split(",");
    return Object.fromEntries(head.map((h, i) => [h, (cells[i] || "").trim()]));
  });
}

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
  const [history, setHistory] = useState<RunSummary[]>([]);
  const [historyFor, setHistoryFor] = useState<SavedRun | null>(null);
  const [viewing, setViewing] = useState<RunDetail | null>(null);
  const [running, setRunning] = useState<SavedRun | null>(null);
  const [runValues, setRunValues] = useState<Record<string, string>>({});
  const [runCsv, setRunCsv] = useState("");
  const [notice, setNotice] = useState("");
  const [newKey, setNewKey] = useState<{ run: string; key: string } | null>(null);

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
    loadHistory(historyFor?.id || "");
    const saved = await fetch("/api/browser/logins", { credentials: "include" });
    const rows = await saved.json().catch(() => []);
    if (saved.ok && Array.isArray(rows)) setLogins(rows);
  }

  async function startRun() {
    if (!running) return;
    const rows = runCsv.trim() ? parseRows(runCsv) : [];
    if (runCsv.trim() && rows.length === 0) {
      setError("Paste a header line with the value names, then one line per run.");
      return;
    }
    setBusy("run");
    setError("");
    try {
      const res = await fetch(`/api/browser/saved-runs/${running.id}/run`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(rows.length ? { values: runValues, rows } : { values: runValues }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(apiError(data, "Could not start the run"));
      setNotice(`Started in the background (${data.rows} run${data.rows === 1 ? "" : "s"}). Follow it in the chat list under “${running.name || running.goal}”.`);
      setRunning(null);
      setRunCsv("");
    } catch (err: any) {
      setError(err.message || "Could not start the run");
    } finally {
      setBusy("");
    }
  }

  async function makeKey(run: SavedRun) {
    if (run.has_run_key && !window.confirm("Make a new key? The old one stops working.")) return;
    setBusy("key");
    const res = await fetch(`/api/browser/saved-runs/${run.id}/key`, { method: "POST", credentials: "include" });
    const data = await res.json().catch(() => ({}));
    setBusy("");
    if (!res.ok || !data.key) {
      setError(apiError(data, "Could not make a key"));
      return;
    }
    setNewKey({ run: run.id, key: data.key });
    await refresh();
  }

  async function loadHistory(traceId: string) {
    const res = await fetch(`/api/browser/runs${traceId ? `?trace_id=${encodeURIComponent(traceId)}` : ""}`, { credentials: "include" });
    const rows = await res.json().catch(() => []);
    if (res.ok && Array.isArray(rows)) setHistory(rows);
  }

  async function openRun(id: string) {
    const res = await fetch(`/api/browser/runs/${id}`, { credentials: "include" });
    const data = await res.json().catch(() => null);
    if (!res.ok || !data) {
      setError(apiError(data, "Could not open that run"));
      return;
    }
    setViewing(data);
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
        row.check
          ? { key: `c${row.index}`, check: row.check.text, yes: row.check.yes, no: row.check.no, text: row.text }
          : row.approve
          ? { key: `a${row.index}`, approve: row.text.replace(/^Wait for your OK: /, ""), text: row.text }
          : row.ai
            ? { key: `g${row.index}`, goal: row.text, text: row.text }
            : { key: `k${row.index}`, keep: row.index, text: row.text },
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
    const steps = draft.map((row) =>
      row.check !== undefined
        ? { check: row.check, yes: row.yes ?? 0, no: row.no ?? 0 }
        : row.approve !== undefined
          ? { approve: row.approve }
          : row.goal !== undefined
            ? { goal: row.goal }
            : { keep: row.keep },
    );
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
          {notice ? <p className="att-summary">{notice}</p> : null}

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
                    <button
                      className="icon-btn"
                      disabled={!!busy}
                      onClick={() => {
                        setRunning(run);
                        setRunValues({});
                        setRunCsv("");
                      }}
                    >
                      Run
                    </button>
                    <button
                      className="icon-btn"
                      title="Runs of this saved run"
                      onClick={() => {
                        setHistoryFor(run);
                        loadHistory(run.id);
                        document.getElementById("run-history")?.scrollIntoView({ behavior: "smooth" });
                      }}
                    >
                      History
                    </button>
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
                      <FlowGraph rows={run.step_rows} />
                      {run.params.length ? (
                        <div className="run-vars">
                          <strong>Variables</strong>
                          <span className="muted">Filled from what you write when you ask for this task (or given when you press Run); left out, a variable keeps its example.</span>
                          <table>
                            <tbody>
                              {run.params.map((p) => (
                                <tr key={p.name}>
                                  <td>
                                    <code>{`{{${p.name}}}`}</code>
                                  </td>
                                  <td>{p.label || ""}</td>
                                  <td className="muted">{p.secret ? "secret (saved sign-in)" : p.example ? `e.g. ${p.example}` : ""}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      ) : null}
                      {run.start_url ? (
                        <p className="faint" style={{ fontSize: 12 }}>
                          Starts at {run.start_url}
                        </p>
                      ) : null}
                      <div className="run-api">
                        <strong>Start it from another system</strong>
                        <span className="muted">
                          {run.has_run_key ? "This run has a key. Make a new one if you lost it; the old one stops working." : "Make a key, then call the address below from any tool that can send a web request."}
                        </span>
                        {newKey && newKey.run === run.id ? (
                          <pre className="run-code">{`curl -X POST ${window.location.origin}/api/run-hooks/${run.id} \\
  -H "Authorization: Bearer ${newKey.key}" \\
  -H "Content-Type: application/json" \\
  -d '${JSON.stringify({ values: Object.fromEntries(run.params.filter((p) => !p.secret && !p.name.startsWith("login_")).map((p) => [p.name, "…"])) })}'`}</pre>
                        ) : null}
                        {newKey && newKey.run === run.id ? <span className="faint">Copy the key now; it is not shown again.</span> : null}
                        <div className="row">
                          <button className="btn secondary" disabled={!!busy} onClick={() => makeKey(run)}>
                            {run.has_run_key ? "New key" : "Make a key"}
                          </button>
                          {run.has_run_key ? (
                            <button className="btn secondary" disabled={!!busy} onClick={() => call(run.id, "/key", "DELETE")}>
                              Remove key
                            </button>
                          ) : null}
                        </div>
                      </div>
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
                        value you give each run. A check asks a yes/no question about the page and jumps to the step you pick.
                      </p>
                      <ol className="run-steps editing">
                        {draft.map((row, i) => (
                          <li key={row.key}>
                            {row.check !== undefined ? (
                              <span className="run-check-edit">
                                <input
                                  className="field"
                                  value={row.check}
                                  placeholder="A yes/no question about the page, e.g. Does it say no flights found?"
                                  onChange={(e) => setDraft((rows) => rows.map((r, j) => (j === i ? { ...r, check: e.target.value } : r)))}
                                />
                                {(["yes", "no"] as const).map((branch) => (
                                  <label key={branch} className="run-branch">
                                    {branch} →
                                    <select
                                      className="field"
                                      value={String(row[branch] ?? 0)}
                                      onChange={(e) => setDraft((rows) => rows.map((r, j) => (j === i ? { ...r, [branch]: Number(e.target.value) } : r)))}
                                    >
                                      <option value="0">End</option>
                                      {draft.map((_, n) => (n === i ? null : <option key={n} value={n + 1}>{`Step ${n + 1}`}</option>))}
                                    </select>
                                  </label>
                                ))}
                              </span>
                            ) : row.approve !== undefined ? (
                              <input
                                className="field"
                                value={row.approve}
                                placeholder="What should you check before it goes on?"
                                onChange={(e) => setDraft((rows) => rows.map((r, j) => (j === i ? { ...r, approve: e.target.value } : r)))}
                              />
                            ) : row.goal !== undefined ? (
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
                              {row.goal === undefined && row.approve === undefined && row.check === undefined ? (
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
                        <button
                          type="button"
                          className="btn secondary"
                          onClick={() => setDraft((rows) => [...rows, { key: `p${Date.now()}`, approve: "Check the page, then continue?", text: "" }])}
                        >
                          Add approval step
                        </button>
                        <button
                          type="button"
                          className="btn secondary"
                          onClick={() =>
                            setDraft((rows) => [...rows, { key: `c${Date.now()}`, check: "", yes: 0, no: 0, text: "" }])
                          }
                        >
                          Add check
                        </button>
                        <span style={{ flex: 1 }} />
                        <button type="button" className="btn secondary" onClick={() => setEditing("")}>
                          Cancel
                        </button>
                        <button
                          type="button"
                          className="btn"
                          disabled={
                            !!busy ||
                            draft.length === 0 ||
                            draft.some((r) => (r.goal !== undefined && !r.goal.trim()) || (r.check !== undefined && !r.check.trim()))
                          }
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

          <div className="page-hero" id="run-history" style={{ marginTop: 28 }}>
            <div>
              <h2>{historyFor ? `Runs of “${historyFor.name || historyFor.goal}”` : "Recent runs"}</h2>
              <p className="muted">Every browser task, step by step: what was done, why, and what the screen looked like. Open one to see where it stopped.</p>
            </div>
            {historyFor ? (
              <button
                className="btn secondary"
                onClick={() => {
                  setHistoryFor(null);
                  loadHistory("");
                }}
              >
                Show all runs
              </button>
            ) : null}
          </div>
          <div className="task-list">
            {history.length === 0 ? <p className="muted">No runs yet.</p> : null}
            {history.map((h) => (
              <button key={h.id} className="task-card run-row ghost" onClick={() => openRun(h.id)}>
                <div className="task-main">
                  <strong>{h.goal.split("\n")[0].slice(0, 140)}</strong>
                  <div className="task-meta">
                    <span className={`run-status ${h.status}`}>{RUN_STATUS[h.status] || h.status}</span>
                    {h.host ? <span className="app-pill">{h.host}</span> : null}
                    <span className="muted">
                      {h.steps} step{h.steps === 1 ? "" : "s"}
                      {h.failed_steps ? ` · ${h.failed_steps} failed` : ""}
                    </span>
                    <span className="faint">{h.started_at ? new Date(h.started_at).toLocaleString() : ""}</span>
                  </div>
                </div>
              </button>
            ))}
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
      {running ? (
        <div className="sheet run-sheet" onClick={() => setRunning(null)}>
          <form
            className="panel stack"
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => {
              e.preventDefault();
              startRun();
            }}
          >
            <strong>Run “{running.name || running.goal}”</strong>
            <p className="muted">It runs in the background and shows up in your chats. Sign-ins come from your saved sign-ins.</p>
            {running.params
              .filter((p) => !p.secret && !p.name.startsWith("login_"))
              .map((p) => (
                <label key={p.name} className="stack">
                  <span className="muted">{p.name.replace(/_/g, " ")}</span>
                  <input className="field" value={runValues[p.name] || ""} onChange={(e) => setRunValues({ ...runValues, [p.name]: e.target.value })} />
                </label>
              ))}
            <label className="stack">
              <span className="muted">Or run it once per row (up to 50): a header line with the value names, then one line per run</span>
              <textarea
                className="field"
                rows={4}
                placeholder={`${running.params.filter((p) => !p.secret && !p.name.startsWith("login_")).map((p) => p.name).join(",") || "name"}\n…`}
                value={runCsv}
                onChange={(e) => setRunCsv(e.target.value)}
              />
            </label>
            <div className="row">
              <span style={{ flex: 1 }} />
              <button type="button" className="btn secondary" onClick={() => setRunning(null)}>
                Cancel
              </button>
              <button className="btn" disabled={busy === "run"}>
                {busy === "run" ? "Starting…" : runCsv.trim() ? `Run ${parseRows(runCsv).length || ""} times` : "Run"}
              </button>
            </div>
          </form>
        </div>
      ) : null}
      {viewing ? (
        <div className="sheet run-sheet" onClick={() => setViewing(null)}>
          <div className="panel run-viewer" onClick={(e) => e.stopPropagation()}>
            <div className="row">
              <div style={{ flex: 1 }}>
                <strong>{viewing.goal.split("\n")[0].slice(0, 160)}</strong>
                <div className="task-meta">
                  <span className={`run-status ${viewing.status}`}>{RUN_STATUS[viewing.status] || viewing.status}</span>
                  {viewing.host ? <span className="app-pill">{viewing.host}</span> : null}
                  <span className="faint">{viewing.started_at ? new Date(viewing.started_at).toLocaleString() : ""}</span>
                </div>
              </div>
              <button className="icon-btn" onClick={() => setViewing(null)} aria-label="Close">
                ×
              </button>
            </div>
            {viewing.result ? <div className="att-summary">{viewing.result}</div> : null}
            <ol className="run-timeline">
              {viewing.step_rows.map((s) => (
                <li key={s.i} className={s.ok ? "" : "bad"}>
                  <div className="run-step-text">
                    <strong>{s.action}</strong>
                    {s.why ? <span className="muted">{s.why}</span> : null}
                    {s.error ? <span className="err">{s.error}</span> : null}
                    {(s.warnings || []).map((w) => (
                      <span key={w} className="faint">
                        {w}
                      </span>
                    ))}
                    {s.title || s.url ? <span className="faint">{s.title || s.url}</span> : null}
                  </div>
                  {s.shot_url ? (
                    <a href={s.shot_url} target="_blank" rel="noreferrer">
                      <img src={s.shot_url} alt={`Screen after step ${s.i}`} loading="lazy" />
                    </a>
                  ) : null}
                </li>
              ))}
            </ol>
          </div>
        </div>
      ) : null}
    </AppShell>
  );
}
