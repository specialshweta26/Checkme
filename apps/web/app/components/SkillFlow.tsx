"use client";

/** A skill drawn as a flow of nodes: each step is a box, linked to the step after it. In a run, passed steps are ticked
 * (they never run again), the current one is highlighted, and the rest wait. */

export type FlowNode = {
  id: string;
  n: number;
  title: string;
  kind?: "step" | "wait" | string;
  status?: "pending" | "passed" | string;
  done_when?: string;
  evidence?: string;
  next?: string[];
  do?: string;
  branches?: { to: string; when: string }[];
};

export type Flow = { current?: string | null; nodes: FlowNode[] };

export default function SkillFlow({ flow, compact = false }: { flow?: Flow | null; compact?: boolean }) {
  const nodes = flow?.nodes || [];
  if (!nodes.length) return null;
  const passed = nodes.filter((n) => n.status === "passed").length;
  const running = nodes.some((n) => n.status);
  return (
    <div className={`skill-flow${compact ? " compact" : ""}`} aria-label="Skill steps as a flow">
      {running ? (
        <div className="skill-flow-head muted">
          {passed} of {nodes.length} steps passed{flow?.current ? "" : " · finished"}
        </div>
      ) : null}
      <ol className="skill-flow-nodes">
        {nodes.map((node, i) => {
          const state = node.status === "passed" ? "passed" : node.status === "skipped" ? "skipped" : flow?.current === node.id ? "current" : running ? "pending" : "idle";
          return (
            <li key={node.id} className={`skill-node ${state}${node.kind === "wait" ? " wait" : ""}`}>
              <div className="skill-node-box" title={node.done_when ? `Done when: ${node.done_when}` : undefined}>
                <span className="skill-node-mark" aria-hidden>
                  {state === "passed" ? "✓" : node.kind === "wait" ? "⏳" : node.n}
                </span>
                <span className="skill-node-text">
                  <span className="skill-node-title">{node.title}</span>
                  {!compact && node.done_when ? <span className="skill-node-sub muted">Done when: {node.done_when}</span> : null}
                  {state === "passed" && node.evidence ? <span className="skill-node-sub ok">Seen: {node.evidence}</span> : null}
                  {state === "current" ? <span className="skill-node-sub now">Now</span> : null}
                  {node.status === "skipped" ? <span className="skill-node-sub muted">Skipped by a branch</span> : null}
                  {(node.branches || []).map((b) => (
                    <span key={b.to + b.when} className="skill-node-sub branch">
                      ↳ If {b.when}: go to step {Number(String(b.to).replace(/^n/, "")) || b.to}
                    </span>
                  ))}
                </span>
              </div>
              {i < nodes.length - 1 ? <span className="skill-node-link" aria-hidden /> : null}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
