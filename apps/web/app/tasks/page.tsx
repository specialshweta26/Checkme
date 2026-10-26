"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { AppShell } from "../components/AppShell";
import { getMe, loadWorkspace, rememberedScope, type Me, type WorkspaceAgent } from "../lib";

type Task = {
  id: string;
  agent_id?: string | null;
  agent_name?: string;
  agent_color?: string;
  title: string;
  instruction: string;
  prompt: string;
  schedule_kind: string;
  schedule_label: string;
  next_run_label: string;
  last_run_label: string;
  last_result: string;
  last_error: string;
  status: string;
  connected_apps: string[];
  run_count: number;
};

type Tab = "all" | "active" | "paused";

export default function TasksPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [tab, setTab] = useState<Tab>("all");
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [expanded, setExpanded] = useState("");
  const [agents, setAgents] = useState<WorkspaceAgent[]>([]);
  const [agentFilter, setAgentFilter] = useState("all");
  const [createAgentId, setCreateAgentId] = useState("");

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/");
        return;
      }
      setMe(u);
    });
  }, []);

  useEffect(() => {
    loadWorkspace().then((roster) => {
      const rows = roster.agents || [];
      setAgents(rows);
      const remembered = rememberedScope().agent;
      const known = rows.some((agent) => agent.id === remembered);
      const fallback = roster.default_agent_id || rows[0]?.id || "";
      setCreateAgentId((current) => current || (known ? remembered : fallback));
    });
  }, []);

  async function refresh() {
    const agentQuery = agentFilter !== "all" ? `?agent_id=${encodeURIComponent(agentFilter)}` : "";
    const res = await fetch(`/api/tasks${agentQuery}`, { credentials: "include" });
    if (res.status === 401) {
      window.location.replace("/");
      return;
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setError(typeof data.detail === "string" ? data.detail : "Could not load tasks");
      return;
    }
    setTasks(data.tasks || []);
  }

  useEffect(() => {
    if (!me) return;
    refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => window.clearInterval(timer);
  }, [me, agentFilter]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return tasks.filter((t) => {
      if (tab === "active" && t.status !== "active" && t.status !== "running") return false;
      if (tab === "paused" && t.status !== "paused") return false;
      if (!q) return true;
      const blob = `${t.title} ${t.instruction} ${t.prompt} ${t.schedule_label} ${t.agent_name || ""} ${t.connected_apps.join(" ")}`.toLowerCase();
      return blob.includes(q);
    });
  }, [tasks, tab, query]);

  async function create(e: FormEvent) {
    e.preventDefault();
    if (!prompt.trim() || busy) return;
    setBusy("create");
    setError("");
    try {
      const res = await fetch("/api/tasks", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, agent_id: createAgentId || undefined }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not create task");
      setPrompt("");
      setOpen(false);
      await refresh();
    } catch (err: any) {
      setError(err.message || "Could not create task");
    } finally {
      setBusy("");
    }
  }

  async function act(id: string, path: string, method: string, body?: Record<string, string>) {
    setBusy(id + path);
    setError("");
    try {
      const res = await fetch(`/api/tasks/${id}${path}`, {
        method,
        credentials: "include",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Action failed");
      await refresh();
    } catch (err: any) {
      setError(err.message || "Action failed");
    } finally {
      setBusy("");
    }
  }

  return (
    <AppShell me={me} current="tasks">
      <div className="page">
      <div className="tasks-page">
        <div className="page-hero">
          <div>
            <h1>Scheduled work</h1>
            <p className="muted">
              Work that should run on a clock. Each agent keeps its own routines, memory, and persona.
            </p>
          </div>
          <button className="btn" onClick={() => setOpen(true)}>
            New task
          </button>
        </div>

        <div className="tasks-toolbar">
          <div className="tasks-tabs">
            {(["all", "active", "paused"] as Tab[]).map((id) => (
              <button key={id} className={`pill${tab === id ? " on" : ""}`} onClick={() => setTab(id)}>
                {id === "all" ? "All tasks" : id === "active" ? "Active" : "Paused"}
              </button>
            ))}
          </div>
          <div className="tasks-filters">
            <select className="tasks-agent" value={agentFilter} onChange={(e) => setAgentFilter(e.target.value)} aria-label="Filter by agent">
              <option value="all">All agents</option>
              {agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name}
                </option>
              ))}
            </select>
            <input
              className="tasks-search"
              type="search"
              placeholder="Search tasks"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
        </div>

        {error ? <p className="err">{error}</p> : null}

        <div className="task-list">
          {visible.length === 0 ? (
            <div className="task-empty">
              <p className="muted">
                {tasks.length === 0
                  ? "Nothing scheduled yet. Describe the work in plain English — for example, “Every weekday at 10:00 AM IST, review my calendar and email me notes.”"
                  : "No tasks match this filter."}
              </p>
              {tasks.length === 0 ? (
                <button className="btn" onClick={() => setOpen(true)}>
                  Create from chat language
                </button>
              ) : null}
            </div>
          ) : (
            visible.map((t) => {
              const openRow = expanded === t.id;
              return (
                <div key={t.id} className="task-card">
                  <button className="task-main ghost" onClick={() => setExpanded(openRow ? "" : t.id)}>
                    <strong>{t.title}</strong>
                    <span className="muted">{t.instruction}</span>
                    <div className="task-meta">
                      <span className={`status-dot ${t.status}`}>{labelStatus(t.status)}</span>
                      {t.agent_name ? (
                        <span className="agent-chip">
                          <i style={{ background: t.agent_color || "#f97316" }} />
                          {t.agent_name}
                        </span>
                      ) : null}
                      <span className="muted">{t.next_run_label}</span>
                      <span className="faint">{t.schedule_label}</span>
                      <div className="task-apps">
                        {t.connected_apps.map((app) => (
                          <span key={app} className="app-pill">
                            {app}
                          </span>
                        ))}
                      </div>
                    </div>
                  </button>
                  <div className="task-actions">
                    <button
                      className="icon-btn"
                      title="Run now"
                      disabled={!!busy || t.status === "running"}
                      onClick={() => act(t.id, "/run", "POST")}
                    >
                      Run
                    </button>
                    {t.status === "paused" ? (
                      <button
                        className="icon-btn"
                        title="Resume"
                        disabled={!!busy}
                        onClick={() => act(t.id, "", "PATCH", { status: "active" })}
                      >
                        Resume
                      </button>
                    ) : t.status !== "completed" ? (
                      <button
                        className="icon-btn"
                        title="Pause"
                        disabled={!!busy || t.status === "running"}
                        onClick={() => act(t.id, "", "PATCH", { status: "paused" })}
                      >
                        Pause
                      </button>
                    ) : null}
                    <button className="icon-btn" title="Delete" disabled={!!busy} onClick={() => act(t.id, "", "DELETE")}>
                      Delete
                    </button>
                  </div>
                  {openRow ? (
                    <div className="task-detail">
                      {t.last_error ? <p className="err">{t.last_error}</p> : null}
                      {t.last_result ? <div className="att-summary">{t.last_result}</div> : <p className="muted">No run yet.</p>}
                      <p className="muted" style={{ fontSize: 12 }}>
                        Last run {t.last_run_label} · {t.run_count} run{t.run_count === 1 ? "" : "s"}
                      </p>
                    </div>
                  ) : null}
                </div>
              );
            })
          )}
        </div>
      </div>

      {open ? (
        <div className="sheet" onClick={() => setOpen(false)}>
          <form className="panel stack" onClick={(e) => e.stopPropagation()} onSubmit={create}>
            <strong>Create a task</strong>
            <p className="muted">Describe the work and the schedule in plain English. Times are India Standard Time. The task runs as the agent you pick.</p>
            {agents.length ? (
              <label className="stack">
                <span className="muted">Agent</span>
                <select className="field" value={createAgentId} onChange={(e) => setCreateAgentId(e.target.value)}>
                  {agents.map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <textarea
              className="field"
              required
              autoFocus
              placeholder="Every evening at 6:30 PM, pull the latest numbers from my connected apps and email me a short brief."
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
            {error && open ? <p className="err">{error}</p> : null}
            <div className="row">
              <button type="button" className="btn secondary" onClick={() => setOpen(false)}>
                Cancel
              </button>
              <button className="btn" disabled={busy === "create" || !prompt.trim()}>
                {busy === "create" ? "Creating…" : "Create task"}
              </button>
            </div>
          </form>
        </div>
      ) : null}
      </div>
    </AppShell>
  );
}

function labelStatus(status: string) {
  if (status === "active") return "Active";
  if (status === "paused") return "Paused";
  if (status === "completed") return "Completed";
  if (status === "running") return "Running";
  return status;
}
