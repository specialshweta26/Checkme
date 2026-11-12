"use client";

import { useState } from "react";
import type { Flow, FlowNode } from "./SkillFlow";

type Branch = { to: string; when: string };
type Draft = { key: string; title: string; do: string; done_when: string; kind: string; branches: Branch[] };

let seq = 0;
const newKey = () => `k${Date.now().toString(36)}${(seq++).toString(36)}`;

function toDrafts(flow?: Flow | null): Draft[] {
  const nodes = (flow?.nodes || []) as (FlowNode & { do?: string; branches?: Branch[] })[];
  // Branches point at node ids; give every node a stable key so they survive reordering.
  const keys = new Map(nodes.map((n) => [n.id, newKey()]));
  return nodes.map((n) => ({
    key: keys.get(n.id) || newKey(),
    title: n.title || "",
    do: n.do || n.title || "",
    done_when: n.done_when || "",
    kind: n.kind === "wait" ? "wait" : "step",
    branches: (n.branches || []).map((b) => ({ to: keys.get(b.to) || "", when: b.when })).filter((b) => b.to),
  }));
}

/** Edit a skill's flow: steps in order, each with what to do, "done when", step or wait, and forward branches.
 * Branches only go forward: a passed step never runs again, so a loop back can't happen. */
export default function FlowEditor({
  flow,
  busy,
  onSave,
  onCancel,
}: {
  flow?: Flow | null;
  busy?: boolean;
  onSave: (flow: { nodes: { id: string; title: string; do: string; done_when: string; kind: string; branches: Branch[] }[] }) => Promise<void>;
  onCancel: () => void;
}) {
  const [steps, setSteps] = useState<Draft[]>(() => toDrafts(flow));
  const [error, setError] = useState("");

  const change = (i: number, patch: Partial<Draft>) => setSteps((all) => all.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  const move = (i: number, by: number) =>
    setSteps((all) => {
      const j = i + by;
      if (j < 0 || j >= all.length) return all;
      const next = [...all];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  const remove = (i: number) =>
    setSteps((all) => {
      const gone = all[i].key;
      return all.filter((_, j) => j !== i).map((s) => ({ ...s, branches: s.branches.filter((b) => b.to !== gone) }));
    });
  const addAfter = (i: number) =>
    setSteps((all) => [...all.slice(0, i + 1), { key: newKey(), title: "", do: "", done_when: "", kind: "step", branches: [] }, ...all.slice(i + 1)]);

  async function save() {
    setError("");
    const index = new Map(steps.map((s, i) => [s.key, i]));
    for (const [i, s] of steps.entries()) {
      if (!s.do.trim() && !s.title.trim()) return setError(`Step ${i + 1} needs something to do.`);
      for (const b of s.branches) {
        const to = index.get(b.to);
        if (to === undefined || !b.when.trim()) return setError(`Step ${i + 1} has a branch without a condition or a step to go to.`);
        if (to <= i) return setError(`Step ${i + 1}'s branch must go to a later step.`);
      }
    }
    try {
      await onSave({
        nodes: steps.map((s) => ({ id: s.key, title: s.title.trim(), do: s.do.trim() || s.title.trim(), done_when: s.done_when.trim(), kind: s.kind, branches: s.branches })),
      });
    } catch (err: any) {
      setError(err?.message || "Could not save the flow");
    }
  }

  return (
    <div className="flow-editor">
      <ol className="flow-edit-list">
        {steps.map((s, i) => {
          const later = steps.slice(i + 1);
          return (
            <li key={s.key} className={`flow-edit-step${s.kind === "wait" ? " wait" : ""}`}>
              <div className="flow-edit-head">
                <span className="skill-node-mark" aria-hidden>{i + 1}</span>
                <input
                  className="input"
                  value={s.title}
                  placeholder="Short title"
                  aria-label={`Step ${i + 1} title`}
                  onChange={(e) => change(i, { title: e.target.value })}
                />
                <select className="input" value={s.kind} aria-label={`Step ${i + 1} type`} onChange={(e) => change(i, { kind: e.target.value })}>
                  <option value="step">Step</option>
                  <option value="wait">Wait until ready</option>
                </select>
              </div>
              <textarea
                rows={2}
                value={s.do}
                placeholder="What to do"
                aria-label={`Step ${i + 1} what to do`}
                onChange={(e) => change(i, { do: e.target.value })}
              />
              <input
                className="input"
                value={s.done_when}
                placeholder="Done when… (what the screen shows once it worked)"
                aria-label={`Step ${i + 1} done when`}
                onChange={(e) => change(i, { done_when: e.target.value })}
              />
              {s.branches.map((b, k) => (
                <div key={k} className="flow-edit-branch">
                  <span className="muted">If</span>
                  <input
                    className="input"
                    value={b.when}
                    placeholder="condition"
                    aria-label={`Step ${i + 1} branch condition`}
                    onChange={(e) => change(i, { branches: s.branches.map((x, m) => (m === k ? { ...x, when: e.target.value } : x)) })}
                  />
                  <span className="muted">go to</span>
                  <select
                    className="input"
                    value={b.to}
                    aria-label={`Step ${i + 1} branch target`}
                    onChange={(e) => change(i, { branches: s.branches.map((x, m) => (m === k ? { ...x, to: e.target.value } : x)) })}
                  >
                    {later.map((t, m) => (
                      <option key={t.key} value={t.key}>
                        Step {i + 2 + m}: {(t.title || t.do).slice(0, 40)}
                      </option>
                    ))}
                  </select>
                  <button type="button" className="btn secondary small" onClick={() => change(i, { branches: s.branches.filter((_, m) => m !== k) })}>
                    Remove
                  </button>
                </div>
              ))}
              <div className="flow-edit-actions">
                <button type="button" className="btn secondary small" disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Move step ${i + 1} up`}>
                  ↑
                </button>
                <button type="button" className="btn secondary small" disabled={i === steps.length - 1} onClick={() => move(i, 1)} aria-label={`Move step ${i + 1} down`}>
                  ↓
                </button>
                <button
                  type="button"
                  className="btn secondary small"
                  disabled={!later.length}
                  title={later.length ? "" : "A branch goes to a later step; add one first"}
                  onClick={() => change(i, { branches: [...s.branches, { to: later[0].key, when: "" }] })}
                >
                  Add branch
                </button>
                <button type="button" className="btn secondary small" onClick={() => addAfter(i)}>
                  Add step after
                </button>
                <button type="button" className="btn secondary small" disabled={steps.length === 1} onClick={() => remove(i)}>
                  Delete
                </button>
              </div>
            </li>
          );
        })}
      </ol>
      {error ? <p className="error">{error}</p> : null}
      <div className="agent-form-actions">
        <button type="button" className="btn" disabled={busy} onClick={save}>
          Save flow
        </button>
        <button type="button" className="btn secondary" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
