"use client";

import { useState } from "react";

// The plan a background task waits on (docs/PLANS.md): steps you can edit, the tools it expects, what will ask for
// your approval, and the likely cost. Nothing runs until you press Start (or reply "go ahead").

export type JobPlan = {
  steps: string[];
  tools: string[];
  approvals: string[];
  estimate: { usd: number; basis: number; days: number } | null;
  state: "proposed" | "approved";
  edited?: boolean;
};

function money(usd: number) {
  if (usd < 0.01) return "under 1¢";
  if (usd < 1) return `about ${Math.round(usd * 100)}¢`;
  return `about $${usd.toFixed(2)}`;
}

export function PlanCard({ plan, onStart, onCancel, busy }: { plan: JobPlan; onStart: (steps: string[]) => void; onCancel: () => void; busy: string }) {
  const [editing, setEditing] = useState(false);
  const [steps, setSteps] = useState<string[]>(plan.steps);
  const set = (i: number, v: string) => setSteps((s) => s.map((x, j) => (j === i ? v : x)));
  const move = (i: number, d: number) =>
    setSteps((s) => {
      const t = [...s];
      const k = i + d;
      if (k < 0 || k >= t.length) return s;
      [t[i], t[k]] = [t[k], t[i]];
      return t;
    });
  const clean = steps.map((s) => s.trim()).filter(Boolean);

  return (
    <section className="plan-card" aria-label="Plan to approve">
      <div className="plan-card-head">
        <strong>Plan</strong>
        <span className="faint">Nothing runs until you start it</span>
        <button className="btn secondary small" type="button" onClick={() => (editing ? (setSteps(plan.steps), setEditing(false)) : setEditing(true))}>
          {editing ? "Undo edits" : "Edit steps"}
        </button>
      </div>
      {editing ? (
        <ol className="plan-steps editing">
          {steps.map((s, i) => (
            <li key={i}>
              <span className="plan-num">{i + 1}</span>
              <input value={s} maxLength={200} onChange={(e) => set(i, e.target.value)} aria-label={`Step ${i + 1}`} />
              <button type="button" className="icon-btn" aria-label="Move up" disabled={i === 0} onClick={() => move(i, -1)}>
                ↑
              </button>
              <button type="button" className="icon-btn" aria-label="Move down" disabled={i === steps.length - 1} onClick={() => move(i, 1)}>
                ↓
              </button>
              <button type="button" className="icon-btn" aria-label="Remove step" disabled={steps.length <= 1} onClick={() => setSteps((x) => x.filter((_, j) => j !== i))}>
                ×
              </button>
            </li>
          ))}
          {steps.length < 12 ? (
            <li>
              <button type="button" className="btn secondary small" onClick={() => setSteps((x) => [...x, ""])}>
                + Add a step
              </button>
            </li>
          ) : null}
        </ol>
      ) : (
        <ol className="plan-steps">
          {steps.map((s, i) => (
            <li key={i}>
              <span className="plan-num">{i + 1}</span>
              <span>{s}</span>
            </li>
          ))}
        </ol>
      )}
      <dl className="plan-facts">
        {plan.tools?.length ? (
          <div>
            <dt>Uses</dt>
            <dd>
              {plan.tools.map((t) => (
                <span key={t} className="plan-chip">
                  {t}
                </span>
              ))}
            </dd>
          </div>
        ) : null}
        <div>
          <dt>Asks you first</dt>
          <dd>{plan.approvals?.length ? plan.approvals.join(" · ") : "Nothing expected — it will still ask before anything sensitive."}</dd>
        </div>
        <div>
          <dt>Likely cost</dt>
          <dd>
            {plan.estimate ? (
              <>
                {money(plan.estimate.usd)} <span className="faint">(your average over {plan.estimate.basis} background task{plan.estimate.basis === 1 ? "" : "s"})</span>
              </>
            ) : (
              <span className="faint">No estimate yet — shown once you’ve run a few background tasks.</span>
            )}
          </dd>
        </div>
      </dl>
      <div className="row">
        <button className="btn small" type="button" disabled={!!busy || !clean.length} onClick={() => onStart(clean)}>
          {busy === "plan" ? "Starting…" : "Start"}
        </button>
        <button className="btn secondary small" type="button" disabled={!!busy} onClick={onCancel}>
          Don’t run it
        </button>
      </div>
    </section>
  );
}
