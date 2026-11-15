"use client";

import { useEffect, useState } from "react";
import { Select } from "./Select";
import { listHandoffs, relativeTime, setHandoffRule, type Handoff, type HandoffRule, type HandoffStatus, type WorkspaceAgent } from "../lib";

// Agent-to-agent handoffs (docs/HANDOFFS.md), in the agent's About panel: who may hand this agent work, and a receipt
// for each recent handoff to or from it (sent, picked up, finished, or refused by the rule).

const MODES: { value: HandoffRule["mode"]; label: string }[] = [
  { value: "anyone", label: "Any of your agents" },
  { value: "selected", label: "Only the agents I pick" },
  { value: "nobody", label: "Only you" },
];
const STATUS: Record<HandoffStatus, string> = {
  sent: "Sent",
  received: "Working on it",
  done: "Done",
  failed: "Didn’t finish",
  cancelled: "Cancelled",
  refused: "Refused",
};

export function AgentHandoffs({ agent, agents }: { agent: WorkspaceAgent; agents: WorkspaceAgent[] }) {
  const saved: HandoffRule = agent.accepts_from || { mode: "anyone", agents: [] };
  const [rule, setRule] = useState<HandoffRule>(saved);
  const [stored, setStored] = useState<HandoffRule>(saved);
  const [items, setItems] = useState<Handoff[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const others = agents.filter((a) => a.id !== agent.id && !a.archived);

  useEffect(() => {
    const r = agent.accepts_from || { mode: "anyone", agents: [] };
    setRule(r);
    setStored(r);
    setItems(null);
    setError("");
    setNotice("");
    listHandoffs(agent.id, 8).then((d) => setItems(d.messages)).catch(() => setItems([]));
  }, [agent.id]);

  const dirty = rule.mode !== stored.mode || rule.agents.slice().sort().join() !== stored.agents.slice().sort().join();

  async function save(next: HandoffRule) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const out = await setHandoffRule(agent.id, next);
      setRule(out.accepts_from);
      setStored(out.accepts_from);
      setNotice("Saved.");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  function pickMode(mode: HandoffRule["mode"]) {
    const next = { mode, agents: mode === "selected" ? rule.agents : [] };
    setRule(next);
    if (mode !== "selected") void save(next);
  }

  const toggle = (id: string) => setRule((r) => ({ ...r, agents: r.agents.includes(id) ? r.agents.filter((a) => a !== id) : [...r.agents, id] }));

  return (
    <div className="handoffs">
      <p className="about-label">Handoffs</p>
      <div className="handoffs-rule">
        <span>Who can hand {agent.name} work</span>
        <Select value={rule.mode} label={`Who can hand ${agent.name} work`} options={MODES} onChange={(v) => pickMode(v as HandoffRule["mode"])} disabled={busy} />
      </div>
      {rule.mode === "selected" ? (
        others.length ? (
          <div className="handoffs-pick">
            {others.map((a) => (
              <label key={a.id} className="handoffs-agent">
                <input type="checkbox" checked={rule.agents.includes(a.id)} onChange={() => toggle(a.id)} />
                <span>{a.name}</span>
              </label>
            ))}
            <button className="btn small" type="button" disabled={busy || !dirty || !rule.agents.length} onClick={() => save(rule)}>
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        ) : (
          <p className="faint about-note">You have no other agents yet.</p>
        )
      ) : null}
      <p className="faint about-note">
        {rule.mode === "nobody"
          ? `Other agents can’t delegate to ${agent.name} or ask it in a group. You can always message it.`
          : "You can always message it. A refused handoff is listed below, and nothing runs."}
      </p>
      {error ? <p className="err">{error}</p> : null}
      {notice && !dirty ? <p className="faint about-note">{notice}</p> : null}

      {items === null ? (
        <p className="faint about-note">Loading handoffs…</p>
      ) : items.length ? (
        <ul className="handoffs-list" aria-label="Recent handoffs">
          {items.map((h) => {
            const out = h.from_agent_id === agent.id;
            const peer = out ? h.to_name : h.from_name;
            return (
              <li key={h.id} className={`handoff status-${h.status}`}>
                <button type="button" className="handoff-row" aria-expanded={open === h.id} onClick={() => setOpen(open === h.id ? null : h.id)}>
                  <span className="handoff-dir">{out ? `To ${peer || "an agent"}` : `From ${peer || "an agent"}`}</span>
                  <span className={`handoff-status ${h.status}`}>{STATUS[h.status] || h.status}</span>
                  <span className="handoff-task">{h.task}</span>
                  <span className="faint handoff-when">
                    {h.channel === "teammate" ? "In a group" : "Background task"} · {h.created_at ? relativeTime(h.created_at) : ""}
                  </span>
                </button>
                {open === h.id ? (
                  <div className="handoff-detail">
                    <ol className="handoff-steps">
                      <li className="on">Sent {h.created_at ? relativeTime(h.created_at) : ""}</li>
                      {h.status === "refused" ? (
                        <li className="bad">Refused by {h.to_name}’s rule</li>
                      ) : (
                        <>
                          <li className={h.received_at ? "on" : ""}>{h.received_at ? `Picked up ${relativeTime(h.received_at)}` : "Not picked up yet"}</li>
                          <li className={h.finished_at ? (h.status === "done" ? "on" : "bad") : ""}>
                            {h.finished_at ? `${STATUS[h.status]} ${relativeTime(h.finished_at)}` : "Not finished yet"}
                          </li>
                        </>
                      )}
                    </ol>
                    {h.reason ? <p className="handoff-reply">{h.reason}</p> : null}
                    {h.reply ? <p className="handoff-reply">{h.reply}</p> : null}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="faint about-note">No handoffs yet. When {agent.name} hands work to another agent, or gets some, the receipt shows here.</p>
      )}
    </div>
  );
}
