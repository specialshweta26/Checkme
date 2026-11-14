"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { AppShell } from "../components/AppShell";
import { AgentAvatar } from "../components/AgentAvatar";
import { RoutineRow, RunRow } from "../components/Routines";
import {
  createRoutine,
  getMe,
  listRoutines,
  loadWorkspace,
  routineRuns,
  routineSummary,
  type Me,
  type Routine,
  type RoutineRun,
  type RoutineSummary,
  type WorkspaceAgent,
} from "../lib";
import { AgentSelect, Select } from "../components/Select";

// Routines (docs/ROUTINES.md): every agent's scheduled work, how its runs went, and each failure in full.
// URL: ?agent=<id>&tab=routines|history&status=succeeded|failed|needs_approval&days=7|30|90

type Tab = "routines" | "history";
const PERIODS = [7, 30, 90];

function readParams() {
  if (typeof window === "undefined") return { agent: "", tab: "routines" as Tab, status: "", days: 30 };
  const q = new URLSearchParams(window.location.search);
  const days = Number(q.get("days"));
  return {
    agent: q.get("agent") || "",
    tab: (q.get("tab") === "history" ? "history" : "routines") as Tab,
    status: q.get("status") || "",
    days: PERIODS.includes(days) ? days : 30,
  };
}

export default function RoutinesPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [agents, setAgents] = useState<WorkspaceAgent[]>([]);
  const [agent, setAgent] = useState("");
  const [tab, setTab] = useState<Tab>("routines");
  const [status, setStatus] = useState("");
  const [days, setDays] = useState(30);
  const [summary, setSummary] = useState<RoutineSummary | null>(null);
  const [tasks, setTasks] = useState<Routine[] | null>(null);
  const [runs, setRuns] = useState<RoutineRun[] | null>(null);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [newFor, setNewFor] = useState("");
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const p = readParams();
    setAgent(p.agent);
    setTab(p.tab);
    setStatus(p.status);
    setDays(p.days);
    setReady(true);
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
      loadWorkspace().then((w) => {
        const rows = (w.agents || []).filter((a) => !a.archived);
        setAgents(rows);
        setNewFor(p.agent || w.default_agent_id || rows[0]?.id || "");
      });
    });
  }, []);

  // Keep the URL in step, so a failure link from the drawer or a teammate opens the same view.
  useEffect(() => {
    if (!ready) return;
    const q = new URLSearchParams();
    if (agent) q.set("agent", agent);
    if (tab === "history") q.set("tab", "history");
    if (status) q.set("status", status);
    if (days !== 30) q.set("days", String(days));
    const text = q.toString();
    window.history.replaceState(null, "", text ? `/routines?${text}` : "/routines");
  }, [ready, agent, tab, status, days]);

  async function refresh() {
    try {
      const [sum, list, history] = await Promise.all([routineSummary(agent || null, days), listRoutines(agent || null), routineRuns({ agentId: agent || null, status, days, limit: 200 })]);
      setSummary(sum);
      setTasks(list.tasks);
      setRuns(history.runs);
      setError("");
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    if (!me || !ready) return;
    void refresh();
    const timer = window.setInterval(refresh, 30000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me, ready, agent, status, days]);

  function showRuns(next: string) {
    setTab("history");
    setStatus(next);
  }

  async function add(e: FormEvent) {
    e.preventDefault();
    if (!prompt.trim()) return;
    setBusy(true);
    try {
      await createRoutine(prompt, newFor || null);
      setPrompt("");
      setAdding(false);
      await refresh();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const byAgent = useMemo(() => {
    const groups = new Map<string, { name: string; color: string; id: string; rows: Routine[] }>();
    for (const t of tasks || []) {
      const key = t.agent_id || "";
      if (!groups.has(key)) groups.set(key, { id: key, name: t.agent_name || "Unassigned", color: t.agent_color || "#64748b", rows: [] });
      groups.get(key)!.rows.push(t);
    }
    return [...groups.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [tasks]);
  const agentRow = (id?: string | null) => agents.find((a) => a.id === id);
  const s = summary;
  const tiles: { key: string; label: string; value: string; tone: string; onClick?: () => void; active?: boolean }[] = s
    ? [
        { key: "runs", label: `Runs, last ${days} days`, value: String(s.runs), tone: "neutral", onClick: () => showRuns(""), active: tab === "history" && !status },
        { key: "succeeded", label: "Succeeded", value: String(s.succeeded), tone: "good", onClick: () => showRuns("succeeded"), active: tab === "history" && status === "succeeded" },
        { key: "failed", label: "Failed", value: String(s.failed), tone: s.failed ? "bad" : "neutral", onClick: () => showRuns("failed"), active: tab === "history" && status === "failed" },
        { key: "approval", label: "Waiting for approval", value: String(s.needs_approval), tone: s.needs_approval ? "warn" : "neutral", onClick: () => showRuns("needs_approval"), active: tab === "history" && status === "needs_approval" },
        { key: "rate", label: "Success rate", value: s.success_rate === null ? "–" : `${s.success_rate}%`, tone: s.success_rate === null ? "neutral" : s.success_rate >= 90 ? "good" : s.success_rate >= 70 ? "warn" : "bad" },
        { key: "active", label: "Active routines", value: `${s.active}`, tone: "neutral", onClick: () => setTab("routines"), active: tab === "routines" },
      ]
    : [];
  const rateWidth = s && s.succeeded + s.failed ? (100 * s.succeeded) / (s.succeeded + s.failed) : 0;

  return (
    <AppShell me={me} current="routines">
      <div className="page">
        <div className="tasks-page">
          <div className="page-hero">
            <div>
              <span className="eyebrow">Profile</span>
              <h1>Routines</h1>
              <p className="muted">
                The work your agents do on a schedule, and how every run went. Open a failed run to see what went wrong and the conversation it
                happened in.
              </p>
            </div>
            <div className="routines-hero-actions">
              <div className="skills-target">
                <span>Period</span>
                <Select value={String(days)} onChange={(v) => setDays(Number(v))} label="Period" options={PERIODS.map((d) => ({ value: String(d), label: `Last ${d} days` }))} />
              </div>
              <button className="btn" type="button" onClick={() => setAdding((v) => !v)}>
                New routine
              </button>
            </div>
          </div>

          {adding ? (
            <form className="task-card routine-new" onSubmit={add}>
              <div className="skills-target">
                <span>Agent</span>
                <AgentSelect value={newFor} onChange={setNewFor} label="Agent" agents={agents} />
              </div>
              <label className="routine-new-text">
                <span>What and when</span>
                <input autoFocus placeholder="e.g. every Monday at 9am, summarise last week's sales emails" value={prompt} onChange={(e) => setPrompt(e.target.value)} />
              </label>
              <button className="btn" disabled={busy || !prompt.trim()}>
                {busy ? "Scheduling…" : "Schedule"}
              </button>
            </form>
          ) : null}

          {error ? <p className="err">{error}</p> : null}

          <section className="routine-tiles" aria-label="How runs went">
            {tiles.map((t) =>
              t.onClick ? (
                <button key={t.key} type="button" className={`routine-tile tone-${t.tone}${t.active ? " on" : ""}`} onClick={t.onClick}>
                  <strong>{t.value}</strong>
                  <span>{t.label}</span>
                </button>
              ) : (
                <div key={t.key} className={`routine-tile tone-${t.tone}`}>
                  <strong>{t.value}</strong>
                  <span>{t.label}</span>
                  <span className="rate-bar" aria-hidden>
                    <span style={{ width: `${rateWidth}%` }} />
                  </span>
                </div>
              ),
            )}
          </section>

          {s?.last_failure && status !== "failed" ? (
            <button type="button" className="routine-alert" onClick={() => showRuns("failed")}>
              <strong>Last failure:</strong> {s.last_failure.task_title}
              {s.last_failure.agent_name ? ` (${s.last_failure.agent_name})` : ""} · {s.last_failure.started_label} — {s.last_failure.error || "see the run"}
              <span className="routine-alert-go">Investigate →</span>
            </button>
          ) : null}

          {s && s.agents.length > 1 ? (
            <div className="routine-agents" role="tablist" aria-label="Agent">
              <button type="button" role="tab" aria-selected={!agent} className={`routine-agent${!agent ? " on" : ""}`} onClick={() => setAgent("")}>
                <strong>All agents</strong>
                <span>{s.routines} routines</span>
              </button>
              {s.agents.map((a) => {
                const row = agentRow(a.agent_id);
                return (
                  <button key={a.agent_id || "none"} type="button" role="tab" aria-selected={agent === a.agent_id} className={`routine-agent${agent === a.agent_id ? " on" : ""}`} onClick={() => setAgent(a.agent_id || "")}>
                    <AgentAvatar name={a.agent_name || "?"} color={a.agent_color || row?.color || "#64748b"} avatar={row?.avatar} className="agent-avatar tiny" />
                    <span className="routine-agent-copy">
                      <strong>{a.agent_name || "Unassigned"}</strong>
                      <span>
                        {a.routines} routine{a.routines === 1 ? "" : "s"} · <span className="good">{a.succeeded} ok</span>
                        {a.failed ? <> · <span className="bad">{a.failed} failed</span></> : null}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          ) : agent ? (
            <p className="muted">
              Showing {agentRow(agent)?.name || "one agent"} only.{" "}
              <button type="button" className="link-btn" onClick={() => setAgent("")}>
                Show all agents
              </button>
            </p>
          ) : null}

          <div className="tasks-toolbar">
            <div className="tasks-tabs" role="tablist" aria-label="View">
              <button role="tab" aria-selected={tab === "routines"} className={`pill${tab === "routines" ? " on" : ""}`} onClick={() => setTab("routines")}>
                Routines <span className="pill-count">{tasks?.length ?? 0}</span>
              </button>
              <button role="tab" aria-selected={tab === "history"} className={`pill${tab === "history" ? " on" : ""}`} onClick={() => setTab("history")}>
                Run history <span className="pill-count">{s?.runs ?? 0}</span>
              </button>
            </div>
            {tab === "history" ? (
              <div className="tasks-tabs" role="tablist" aria-label="Outcome">
                {[
                  ["", "All"],
                  ["failed", "Failed"],
                  ["succeeded", "Succeeded"],
                  ["needs_approval", "Waiting"],
                ].map(([id, label]) => (
                  <button key={id || "all"} role="tab" aria-selected={status === id} className={`pill${status === id ? " on" : ""}${id === "failed" ? " pill-bad" : ""}`} onClick={() => setStatus(id)}>
                    {label}
                  </button>
                ))}
              </div>
            ) : null}
          </div>

          {tab === "routines" ? (
            tasks === null ? (
              <p className="muted">Loading…</p>
            ) : tasks.length === 0 ? (
              <div className="task-empty">
                <p className="muted">No routines yet. Ask any agent in chat ("every weekday at 9, brief me"), or add one with New routine.</p>
              </div>
            ) : (
              <div className="routine-groups">
                {byAgent.map((g) => (
                  <section key={g.id} className="routine-group">
                    {!agent ? (
                      <header>
                        <AgentAvatar name={g.name} color={agentRow(g.id)?.color || g.color} avatar={agentRow(g.id)?.avatar} className="agent-avatar tiny" />
                        <strong>{g.name}</strong>
                        <span className="faint">{g.rows.length}</span>
                      </header>
                    ) : null}
                    <div className="routine-list">
                      {g.rows.map((t) => (
                        <RoutineRow key={t.id} task={t} compact onChanged={refresh} onError={setError} />
                      ))}
                    </div>
                  </section>
                ))}
              </div>
            )
          ) : runs === null ? (
            <p className="muted">Loading…</p>
          ) : runs.length === 0 ? (
            <div className="task-empty">
              <p className="muted">{status === "failed" ? `No failed runs in the last ${days} days.` : `No runs in the last ${days} days.`}</p>
            </div>
          ) : (
            <div className="run-list">
              {runs.map((r, i) => (
                <RunRow key={r.id} run={r} showAgent={!agent} startOpen={status === "failed" && i === 0} />
              ))}
            </div>
          )}
        </div>
      </div>
    </AppShell>
  );
}
