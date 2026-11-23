"use client";

import { createContext, useContext, useEffect, useState, type FormEvent } from "react";
import {
  chatHref,
  createRoutine,
  listRoutines,
  routineDelete,
  routineEdit,
  routineRunNow,
  routineSetStatus,
  routineSummary,
  type Routine,
  type RoutineRun,
  type RoutineSummary,
} from "../lib";
import { ConfirmDelete, Triggers } from "./Triggers";
import { Watches } from "./Watches";

// Routines (docs/ROUTINES.md): scheduled tasks, one agent at a time from the clock button or an agent card, and every
// agent on the Routines page. Results open the same conversation on web and desktop.

/** The right-hand panel AppShell owns (an agent's Routines or About), opened from pages inside it like the chat header. */
export type PanelKind = "routines" | "about";
export const PanelContext = createContext<{ open: (kind: PanelKind, agentId: string) => void; current: { kind: string; agentId: string } | null } | null>(null);

/** The clock button for one agent: opens that agent's routines; with no agent, links to every routine. */
export function RoutinesButton({ agentId, agentName, className }: { agentId?: string | null; agentName?: string; className: string }) {
  const ctx = useContext(PanelContext);
  const icon = (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="12" cy="12" r="8" stroke="currentColor" strokeWidth="1.8" />
      <path d="M12 8v4.5l3 2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
  if (!agentId || !ctx) {
    return (
      <a className={className} href="/routines" aria-label="Routines" title="Routines">
        {icon}
      </a>
    );
  }
  const label = `${agentName || "This agent"}'s routines`;
  return (
    <button
      type="button"
      className={`${className}${ctx.current?.kind === "routines" && ctx.current.agentId === agentId ? " on" : ""}`}
      aria-label={label}
      title={label}
      aria-pressed={ctx.current?.kind === "routines" && ctx.current.agentId === agentId}
      onClick={() => ctx.open("routines", agentId)}
    >
      {icon}
    </button>
  );
}

export const RUN_LABEL: Record<string, string> = { succeeded: "Succeeded", failed: "Failed", needs_approval: "Needs approval" };
const ROUTINE_LABEL: Record<string, string> = { active: "Active", running: "Running", paused: "Paused", completed: "Done" };

export function duration(ms: number | null | undefined) {
  if (ms === null || ms === undefined) return "";
  if (ms < 1000) return "under 1s";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

export function resultHref(agentId?: string | null, sessionId?: string | null) {
  return sessionId ? chatHref({ agent: agentId, conversation: sessionId }) : agentId ? chatHref({ agent: agentId }) : "/chat";
}

export function RoutineStatus({ status }: { status: string }) {
  return <span className={`routine-chip st-${status}`}>{ROUTINE_LABEL[status] || status}</span>;
}

export function RunStatus({ status }: { status: string }) {
  return <span className={`run-chip st-${status}`}>{RUN_LABEL[status] || status}</span>;
}

/** One scheduled task with its actions. Used in the drawer and on the Routines page. */
export function RoutineRow({ task, onChanged, onError, compact = false }: { task: Routine; onChanged: () => void; onError: (m: string) => void; compact?: boolean }) {
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(task.title);
  const [instruction, setInstruction] = useState(task.instruction || task.prompt || "");
  const lastFailed = Boolean(task.last_error);

  async function act(kind: string, fn: () => Promise<unknown>) {
    setBusy(kind);
    try {
      await fn();
      onChanged();
    } catch (e: any) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className={`routine-row${open ? " open" : ""}`}>
      <button type="button" className="routine-main" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span className={`routine-dot${task.last_run_at ? (lastFailed ? " bad" : " good") : ""}`} aria-hidden />
        <span className="routine-copy">
          <strong>{task.title}</strong>
          <span className="routine-meta">
            <span>{task.schedule_label}</span>
            {task.status === "active" || task.status === "running" ? <span>Next {task.next_run_label}</span> : null}
            {task.last_run_at ? <span className={lastFailed ? "bad" : ""}>{lastFailed ? "Failed" : "Ran"} {task.last_run_label}</span> : <span className="faint">Not run yet</span>}
            {!compact && task.agent_name ? <span className="faint">{task.agent_name}</span> : null}
          </span>
        </span>
        <RoutineStatus status={task.status} />
      </button>
      {open ? (
        <div className="routine-detail">
          {editing ? (
            <div style={{ display: "grid", gap: 6 }}>
              <input aria-label="Routine name" value={title} onChange={(e) => setTitle(e.target.value)} />
              <textarea aria-label="What it does" rows={4} value={instruction} onChange={(e) => setInstruction(e.target.value)} />
              <p className="faint">It keeps running {task.schedule_label}. To change when, delete it and add a new one.</p>
              <div className="row" style={{ gap: 6 }}>
                <button
                  className="btn small"
                  type="button"
                  disabled={!!busy || !title.trim() || !instruction.trim()}
                  onClick={() => act("save", async () => { await routineEdit(task.id, { title, instruction }); setEditing(false); })}
                >
                  {busy === "save" ? "Saving…" : "Save"}
                </button>
                <button className="btn secondary small" type="button" disabled={!!busy} onClick={() => setEditing(false)}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <p className="routine-instruction">{task.instruction || task.prompt}</p>
          )}
          {task.last_run_at ? (
            <div className={`routine-last${lastFailed ? " bad" : ""}`}>
              <span className="faint">Last run · {task.last_run_label}</span>
              <p>{lastFailed ? task.last_error : task.last_result || "Done."}</p>
            </div>
          ) : null}
          <div className="row">
            <button className="btn small" type="button" disabled={!!busy || task.status === "running"} onClick={() => act("run", () => routineRunNow(task.id))}>
              {busy === "run" || task.status === "running" ? "Running…" : "Run now"}
            </button>
            {task.status === "paused" ? (
              <button className="btn secondary small" type="button" disabled={!!busy} onClick={() => act("st", () => routineSetStatus(task.id, "active"))}>
                Resume
              </button>
            ) : task.status === "active" ? (
              <button className="btn secondary small" type="button" disabled={!!busy} onClick={() => act("st", () => routineSetStatus(task.id, "paused"))}>
                Pause
              </button>
            ) : null}
            {task.last_run_at ? (
              <a className="btn secondary small" href={resultHref(task.agent_id, task.chat_session_id)}>
                Open result
              </a>
            ) : null}
            <span style={{ flex: 1 }} />
            {!editing ? (
              <button className="btn secondary small" type="button" disabled={!!busy} onClick={() => setEditing(true)}>
                Edit
              </button>
            ) : null}
            <ConfirmDelete label={`“${task.title}” and its run history`} busy={busy === "del"} onDelete={() => act("del", () => routineDelete(task.id))} />
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** One run in the history, with its error and result for investigating. */
export function RunRow({ run, showAgent = true, startOpen = false }: { run: RoutineRun; showAgent?: boolean; startOpen?: boolean }) {
  const [open, setOpen] = useState(startOpen);
  return (
    <div className={`run-row st-${run.status}${open ? " open" : ""}`}>
      <button type="button" className="run-main" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <RunStatus status={run.status} />
        <span className="run-copy">
          <strong>{run.task_title}</strong>
          <span className="routine-meta">
            <span>{run.started_label}</span>
            {showAgent && run.agent_name ? <span>{run.agent_name}</span> : null}
            {run.duration_ms !== null && run.trigger !== "earlier" ? <span className="faint">{duration(run.duration_ms)}</span> : null}
            <span className="faint">{run.trigger === "manual" ? "Started by you" : run.trigger === "earlier" ? "Before run history" : "On schedule"}</span>
          </span>
        </span>
        {run.status === "failed" && run.error ? <span className="run-why">{run.error}</span> : null}
      </button>
      {open ? (
        <div className="run-detail">
          {run.error ? (
            <div className="run-error">
              <strong>What went wrong</strong>
              <p>{run.error}</p>
            </div>
          ) : null}
          {run.result ? (
            <div>
              <span className="faint">What the agent said</span>
              <p className="run-result">{run.result}</p>
            </div>
          ) : null}
          {run.status === "needs_approval" ? <p className="faint">This run is waiting for your approval in its conversation.</p> : null}
          <div className="row">
            <a className="btn secondary small" href={resultHref(run.agent_id, run.session_id)}>
              Open conversation
            </a>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** The ⓘ button: opens the agent's About panel (its profile and every option the agent menu has). */
export function AboutButton({ agentId, agentName, className }: { agentId?: string | null; agentName?: string; className: string }) {
  const ctx = useContext(PanelContext);
  if (!agentId || !ctx) return null;
  const on = ctx.current?.kind === "about" && ctx.current.agentId === agentId;
  return (
    <button type="button" className={`${className}${on ? " on" : ""}`} aria-label={`About ${agentName || "this agent"}`} title={`About ${agentName || "this agent"}`} aria-pressed={on} onClick={() => ctx.open("about", agentId)}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
        <circle cx="12" cy="12" r="8.5" />
        <path d="M12 11v5.2M12 7.8v.2" />
      </svg>
    </button>
  );
}

/** The routines panel for one agent: its routines, its last 30 days, and a box to add one. */
export function AgentRoutinesPanel({ agentId, agentName }: { agentId: string; agentName: string }) {
  const [tasks, setTasks] = useState<Routine[] | null>(null);
  const [stats, setStats] = useState<RoutineSummary | null>(null);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function refresh() {
    try {
      const [list, sum] = await Promise.all([listRoutines(agentId), routineSummary(agentId, 30)]);
      setTasks(list.tasks);
      setStats(sum);
    } catch (e: any) {
      setError(e.message);
      setTasks([]);
    }
  }

  useEffect(() => {
    setTasks(null);
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  async function add(e: FormEvent) {
    e.preventDefault();
    if (!prompt.trim()) return;
    setBusy(true);
    setError("");
    try {
      await createRoutine(prompt, agentId);
      setPrompt("");
      await refresh();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const live = (tasks || []).filter((t) => t.status !== "completed");
  const done = (tasks || []).filter((t) => t.status === "completed");
  return (
    <>
        {stats ? (
          <div className="drawer-stats">
            <div>
              <strong>{stats.active}</strong>
              <span>Active</span>
            </div>
            <div className="good">
              <strong>{stats.succeeded}</strong>
              <span>Succeeded</span>
            </div>
            <div className={`bad${stats.failed ? " has" : ""}`}>
              <strong>{stats.failed}</strong>
              <span>Failed</span>
            </div>
          </div>
        ) : null}
        {stats ? <p className="faint drawer-note">Runs in the last 30 days{stats.success_rate !== null ? ` · ${stats.success_rate}% succeeded` : ""}</p> : null}
        <form className="drawer-add" onSubmit={add}>
          <input aria-label={`Schedule something for ${agentName}`} placeholder={`e.g. every weekday at 9am, brief me on my inbox`} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          <button className="btn small" disabled={busy || !prompt.trim()}>
            {busy ? "Adding…" : "Schedule"}
          </button>
        </form>
        {error ? <p className="err">{error}</p> : null}
        <div className="drawer-list">
          {tasks === null ? <p className="muted">Loading…</p> : null}
          {tasks && live.length === 0 ? <p className="muted">{agentName} has no routines yet. Describe one above, with when it should run.</p> : null}
          {live.map((t) => (
            <RoutineRow key={t.id} task={t} compact onChanged={refresh} onError={setError} />
          ))}
          <Triggers agentId={agentId} />
          <Watches agentId={agentId} agentName={agentName} compact />
          {done.length ? (
            <details className="drawer-done">
              <summary>Finished one-off tasks ({done.length})</summary>
              {done.map((t) => (
                <RoutineRow key={t.id} task={t} compact onChanged={refresh} onError={setError} />
              ))}
            </details>
          ) : null}
        </div>
    </>
  );
}
