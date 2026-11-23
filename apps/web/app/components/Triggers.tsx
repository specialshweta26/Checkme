"use client";

import { useEffect, useState } from "react";
import { deleteTrigger, listTriggers, updateTrigger, type Trigger } from "../lib";

// Triggers (docs/LLD.md "Automations that send directly"): "45 minutes before each meeting" and "when mail from X
// arrives". Shown in an agent's Routines panel and on the Routines page, where they can be paused, edited or deleted.

function what(t: Trigger) {
  if (t.trigger_type === "meeting") return `${t.trigger} minutes before each meeting`;
  return `When mail from ${t.trigger} arrives`;
}

/** Delete in two clicks, inside the page: the browser's confirm box doesn't open in the desktop app. */
export function ConfirmDelete({ label, busy, onDelete }: { label: string; busy: boolean; onDelete: () => void }) {
  const [asking, setAsking] = useState(false);
  if (!asking) {
    return (
      <button className="btn secondary small" type="button" disabled={busy} onClick={() => setAsking(true)}>
        Delete
      </button>
    );
  }
  return (
    <span className="row" style={{ gap: 6 }}>
      <span className="faint">Delete {label}?</span>
      <button className="btn small danger" type="button" disabled={busy} onClick={onDelete}>
        {busy ? "Deleting…" : "Yes, delete"}
      </button>
      <button className="btn secondary small" type="button" disabled={busy} onClick={() => setAsking(false)}>
        Keep
      </button>
    </span>
  );
}

function TriggerRow({ t, onChanged, onError }: { t: Trigger; onChanged: () => void; onError: (m: string) => void }) {
  const [busy, setBusy] = useState("");
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(t.name);
  const [action, setAction] = useState(t.action);
  const [trigger, setTrigger] = useState(t.trigger);

  async function act(kind: string, fn: () => Promise<unknown>) {
    setBusy(kind);
    try {
      await fn();
      setEditing(false);
      onChanged();
    } catch (e: any) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className={`watch-row${t.enabled ? "" : " off"}`}>
      <span className={`watch-icon watch-icon-${t.trigger_type === "meeting" ? "page" : "inbox"}`} aria-hidden>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          {t.trigger_type === "meeting" ? <path d="M7 3v3M17 3v3M4 8h16M5 5h14v15H5z" /> : <path d="M4 6h16v12H4zM4 7l8 6 8-6" />}
        </svg>
      </span>
      <div className="watch-main">
        {editing ? (
          <div className="trigger-edit" style={{ display: "grid", gap: 6 }}>
            <input aria-label="Name" value={name} onChange={(e) => setName(e.target.value)} />
            {t.trigger_type === "meeting" ? (
              <label className="faint">
                Minutes before each meeting{" "}
                <input aria-label="Minutes before each meeting" type="number" min={1} max={1440} value={trigger} onChange={(e) => setTrigger(e.target.value)} style={{ width: 80 }} />
              </label>
            ) : (
              <input aria-label="Sender to watch" value={trigger} onChange={(e) => setTrigger(e.target.value)} />
            )}
            <textarea aria-label="What it does" rows={4} value={action} onChange={(e) => setAction(e.target.value)} />
            <div className="row" style={{ gap: 6 }}>
              <button className="btn small" type="button" disabled={!!busy || !name.trim() || !action.trim()} onClick={() => act("save", () => updateTrigger(t.id, { name, action, trigger }))}>
                {busy === "save" ? "Saving…" : "Save"}
              </button>
              <button className="btn secondary small" type="button" disabled={!!busy} onClick={() => setEditing(false)}>
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <>
            <strong>{t.name}</strong>
            <span className="watch-what">{what(t)}</span>
            <span className="watch-change">{t.action}</span>
          </>
        )}
        <span className="watch-status">
          {!t.enabled ? <span className="run-chip">Paused</span> : null}
          <label>
            <input
              type="checkbox"
              checked={!t.requires_approval}
              disabled={!!busy}
              onChange={(e) => act("approval", () => updateTrigger(t.id, { requires_approval: !e.target.checked }))}
            />{" "}
            Send to me without asking
          </label>
        </span>
      </div>
      {!editing ? (
        <div className="watch-actions">
          <button className="btn secondary small" type="button" disabled={!!busy} onClick={() => setEditing(true)}>
            Edit
          </button>
          <button className="btn secondary small" type="button" disabled={!!busy} onClick={() => act("toggle", () => updateTrigger(t.id, { enabled: !t.enabled }))}>
            {t.enabled ? "Pause" : "Resume"}
          </button>
          <ConfirmDelete label={`“${t.name}”`} busy={busy === "del"} onDelete={() => act("del", () => deleteTrigger(t.id))} />
        </div>
      ) : null}
    </div>
  );
}

/** An agent's meeting and mail triggers (every agent's when agentId is empty). Nothing shows when there are none. */
export function Triggers({ agentId }: { agentId?: string | null }) {
  const [items, setItems] = useState<Trigger[] | null>(null);
  const [error, setError] = useState("");

  async function refresh() {
    try {
      setItems(await listTriggers(agentId));
    } catch (e: any) {
      setError(e.message);
      setItems([]);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  if (!items?.length && !error) return null;
  return (
    <section className="watches compact" aria-label="Meeting and mail triggers">
      <div className="watches-head">
        <h3>Meeting and mail triggers</h3>
      </div>
      {error ? <p className="err">{error}</p> : null}
      {(items || []).map((t) => (
        <TriggerRow key={t.id} t={t} onChanged={refresh} onError={setError} />
      ))}
    </section>
  );
}
