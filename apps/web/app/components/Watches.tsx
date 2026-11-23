"use client";

import { useEffect, useState, type FormEvent } from "react";
import { AgentSelect, Select } from "./Select";
import { ConfirmDelete } from "./Triggers";
import { checkWatch, createWatch, deleteWatch, listWatches, relativeTime, setWatchEnabled, type Watch, type WorkspaceAgent } from "../lib";

// Watches (docs/WATCHES.md): an agent wakes when a page changes (or a phrase appears on it), or when the inbox has
// more than N cards waiting. Shown in an agent's Routines panel and on the Routines page.

const EVERY = [15, 30, 60, 180, 360, 720, 1440];
const everyLabel = (m: number) => (m < 60 ? `${m} minutes` : m === 60 ? "hour" : m === 1440 ? "day" : `${m / 60} hours`);

function host(url: string) {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function WatchRow({ w, onChanged, onError, showAgent }: { w: Watch; onChanged: () => void; onError: (m: string) => void; showAgent?: string }) {
  const [busy, setBusy] = useState("");
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
  const s = w.watch;
  const what =
    w.trigger_type === "page"
      ? `${host(w.trigger)}${w.condition ? ` · when “${w.condition}” appears` : " · any change"} · every ${everyLabel(s.every_minutes || 60)}`
      : `Inbox · more than ${w.trigger} cards waiting`;
  return (
    <div className={`watch-row${w.enabled ? "" : " off"}`}>
      <span className={`watch-icon watch-icon-${w.trigger_type}`} aria-hidden>
        {w.trigger_type === "page" ? (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z" />
            <circle cx="12" cy="12" r="3" />
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 13h4l2 3h4l2-3h4M4 13l2-7h12l2 7v5H4z" />
          </svg>
        )}
      </span>
      <div className="watch-main">
        <strong>{w.name}</strong>
        <span className="watch-what">{what}</span>
        <span className="watch-status">
          {showAgent ? <span>{showAgent}</span> : null}
          {!w.enabled ? <span className="run-chip">Paused</span> : null}
          {s.error ? <span className="bad">{s.error}</span> : null}
          {!s.error && s.checked_at ? <span>Checked {relativeTime(s.checked_at)}</span> : null}
          {!s.error && !s.checked_at ? <span className="faint">Not checked yet</span> : null}
          {s.changed_at ? <span className="good">Woke {relativeTime(s.changed_at)}</span> : null}
          {s.fired ? <span className="faint">{s.fired}× so far</span> : null}
        </span>
        {s.last_change?.added?.length ? (
          <span className="watch-change">
            New: {s.last_change.added.slice(0, 2).join(" · ")}
          </span>
        ) : null}
      </div>
      <div className="watch-actions">
        <button className="btn secondary small" type="button" disabled={!!busy || !w.enabled} onClick={() => act("check", () => checkWatch(w.id))}>
          {busy === "check" ? "Checking…" : "Check now"}
        </button>
        <button className="btn secondary small" type="button" disabled={!!busy} onClick={() => act("toggle", () => setWatchEnabled(w.id, !w.enabled))}>
          {w.enabled ? "Pause" : "Resume"}
        </button>
        <ConfirmDelete label={`the watch “${w.name}”`} busy={busy === "del"} onDelete={() => act("del", () => deleteWatch(w.id))} />
      </div>
    </div>
  );
}

/** Watches for one agent (agentId) or all of them, with a form to add one. */
export function Watches({ agentId, agentName, agents, compact = false }: { agentId?: string | null; agentName?: string; agents?: WorkspaceAgent[]; compact?: boolean }) {
  const [rows, setRows] = useState<Watch[] | null>(null);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ trigger_type: "page" as "page" | "inbox", name: "", trigger: "", condition: "", every: "60", action: "", agent: agentId || "" });

  async function refresh() {
    try {
      setRows(await listWatches(agentId || null));
    } catch (e: any) {
      setError(e.message);
      setRows([]);
    }
  }

  useEffect(() => {
    void refresh();
    setForm((f) => ({ ...f, agent: agentId || f.agent || agents?.[0]?.id || "" }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  async function add(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await createWatch({
        trigger_type: form.trigger_type,
        name: form.name.trim() || (form.trigger_type === "page" ? `Watch ${host(form.trigger)}` : "Inbox watch"),
        trigger: form.trigger,
        condition: form.trigger_type === "page" ? form.condition : "",
        every_minutes: Number(form.every),
        action: form.action,
        agent_id: agentId || form.agent || null,
      });
      setAdding(false);
      setForm((f) => ({ ...f, name: "", trigger: "", condition: "", action: "" }));
      await refresh();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const nameOf = (id: string | null) => agents?.find((a) => a.id === id)?.name || "";
  return (
    <section className={`watches${compact ? " compact" : ""}`} aria-label="Watches">
      <div className="watches-head">
        <h3>Watches</h3>
        <button className="btn secondary small" type="button" onClick={() => setAdding((v) => !v)} aria-expanded={adding}>
          {adding ? "Cancel" : "Add a watch"}
        </button>
      </div>
      {!compact ? (
        <p className="muted watches-intro">Wake an agent when something changes, not just on a schedule: a web page, or your inbox filling up.</p>
      ) : null}
      {error ? <p className="err">{error}</p> : null}
      {adding ? (
        <form className="watch-form" onSubmit={add}>
          <div className="watch-type" role="radiogroup" aria-label="Watch">
            {(["page", "inbox"] as const).map((t) => (
              <button key={t} type="button" role="radio" aria-checked={form.trigger_type === t} className={`pill${form.trigger_type === t ? " on" : ""}`} onClick={() => setForm({ ...form, trigger_type: t, trigger: "" })}>
                {t === "page" ? "A web page" : "My inbox"}
              </button>
            ))}
          </div>
          {!agentId && agents?.length ? (
            <label className="vault-field">
              <span>Agent</span>
              <AgentSelect value={form.agent} onChange={(v) => setForm({ ...form, agent: v })} label="Agent" agents={agents.filter((a) => !a.archived)} />
            </label>
          ) : null}
          {form.trigger_type === "page" ? (
            <>
              <label className="vault-field">
                <span>Page address</span>
                <input required placeholder="https://competitor.com/pricing" value={form.trigger} onChange={(e) => setForm({ ...form, trigger: e.target.value })} />
              </label>
              <div className="watch-form-row">
                <label className="vault-field">
                  <span>
                    Wake when <em>optional phrase; empty = any change</em>
                  </span>
                  <input placeholder="e.g. Enterprise plan" value={form.condition} onChange={(e) => setForm({ ...form, condition: e.target.value })} />
                </label>
                <label className="vault-field">
                  <span>Check every</span>
                  <Select value={form.every} onChange={(v) => setForm({ ...form, every: v })} label="Check every" options={EVERY.map((m) => ({ value: String(m), label: everyLabel(m) }))} />
                </label>
              </div>
            </>
          ) : (
            <label className="vault-field">
              <span>Wake when more than this many cards are waiting</span>
              <input required inputMode="numeric" placeholder="20" value={form.trigger} onChange={(e) => setForm({ ...form, trigger: e.target.value.replace(/\D/g, "") })} />
            </label>
          )}
          <label className="vault-field">
            <span>Then {agentName || "the agent"} should</span>
            <textarea required rows={2} placeholder={form.trigger_type === "page" ? "Tell me what changed and whether we should respond." : "Triage the oldest cards and draft replies for the urgent ones."} value={form.action} onChange={(e) => setForm({ ...form, action: e.target.value })} />
          </label>
          <label className="vault-field">
            <span>
              Name <em>optional</em>
            </span>
            <input placeholder={form.trigger_type === "page" ? "Competitor pricing" : "Inbox overflow"} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </label>
          <div className="row">
            <button className="btn small" disabled={busy || !form.trigger.trim() || !form.action.trim()}>
              {busy ? "Adding…" : "Add watch"}
            </button>
            <span className="faint watch-note">Checks cost nothing; the agent only runs when it fires, and asks before anything sensitive.</span>
          </div>
        </form>
      ) : null}
      {rows === null ? <p className="muted">Loading…</p> : null}
      {rows && rows.length === 0 && !adding ? <p className="muted">No watches yet.</p> : null}
      <div className="watch-list">
        {(rows || []).map((w) => (
          <WatchRow key={w.id} w={w} onChanged={refresh} onError={setError} showAgent={!agentId ? nameOf(w.agent_id) : undefined} />
        ))}
      </div>
    </section>
  );
}
