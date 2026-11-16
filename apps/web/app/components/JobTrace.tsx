"use client";

/**
 * "Why it did that": every step of a background job (and the jobs it delegated to), with the agent's reasoning just
 * before each tool call, the inputs (secrets hidden), the result, how long it took and a screenshot for browser and
 * computer steps. `GET /api/jobs/{id}/trace` (docs/RUN_TRACES.md). Refreshes while the job is still running.
 */
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

type TraceStep = {
  id: number;
  job_id: string;
  attempt: number;
  i: number;
  agent_name: string;
  kind: "tool" | "reply";
  tool: string;
  action: string;
  why: string;
  args: Record<string, unknown>;
  result: string;
  ok: boolean;
  error: string;
  ms: number;
  shot_url: string | null;
  browser_run_id: string | null;
  at: string | null;
};
type TraceJob = { id: string; parent_id: string | null; title: string; role: string; agent_name: string; status: string };

function took(ms: number) {
  if (!ms) return "";
  return ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms / 60000)} min`;
}

function pretty(text: string) {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

export function JobTrace({ jobId, live, onClose }: { jobId: string; live: boolean; onClose: () => void }) {
  const [steps, setSteps] = useState<TraceStep[] | null>(null);
  const [jobs, setJobs] = useState<TraceJob[]>([]);
  const [err, setErr] = useState("");
  const [onlyProblems, setOnlyProblems] = useState(false);

  useEffect(() => {
    let stopped = false;
    let timer: number | null = null;
    const load = async () => {
      try {
        const res = await fetch(`/api/jobs/${jobId}/trace`, { credentials: "include", cache: "no-store" });
        if (!res.ok) throw new Error(res.status === 404 ? "This job is gone." : "Could not load the steps");
        const data = await res.json();
        if (!stopped) {
          setSteps(data.steps || []);
          setJobs(data.jobs || []);
          setErr("");
        }
      } catch (e: any) {
        if (!stopped) setErr(e.message || "Could not load the steps");
      }
      if (!stopped && live) timer = window.setTimeout(load, 4000);
    };
    load();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
      window.removeEventListener("keydown", onKey);
    };
  }, [jobId, live]);

  const many = jobs.length > 1 || new Set((steps || []).map((s) => s.agent_name)).size > 1;
  const failed = (steps || []).filter((s) => !s.ok).length;
  const shown = (steps || []).filter((s) => !onlyProblems || !s.ok);
  const root = jobs.find((j) => j.id === jobId) || jobs.find((j) => !j.parent_id);

  // Rendered on the page body: inside the chat's scrolling message list it would be clipped and scroll away.
  return createPortal(
    <div className="sheet run-sheet" onClick={onClose}>
      <div className="panel run-viewer job-trace" role="dialog" aria-label="Why it did that" onClick={(e) => e.stopPropagation()}>
        <div className="row job-trace-head">
          <div style={{ flex: 1 }}>
            <strong>Why it did that</strong>
            <div className="task-meta">
              {root?.title ? <span>{root.title.slice(0, 120)}</span> : null}
              {steps ? (
                <span className="muted">
                  {steps.length} step{steps.length === 1 ? "" : "s"}
                  {failed ? ` · ${failed} didn't work` : ""}
                </span>
              ) : null}
              {live ? <span className="faint">Still working, updating</span> : null}
            </div>
          </div>
          {failed ? (
            <label className="job-trace-filter">
              <input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} /> Only what didn't work
            </label>
          ) : null}
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <p className="muted job-trace-note">
          Each step shows the tool the agent used, what it said or thought just before, what it sent (passwords and keys hidden) and what came back.
        </p>
        {err ? <p className="err">{err}</p> : null}
        {steps === null && !err ? <p className="muted">Loading…</p> : null}
        {steps && steps.length === 0 ? (
          <p className="muted">{live ? "No steps yet. They appear here as the agent works." : "No steps were recorded for this job. Jobs that ran before this view existed have none."}</p>
        ) : null}
        <ol className="run-timeline">
          {shown.map((s) => (
            <li key={s.id} className={`${s.ok ? "" : "bad"} ${s.kind === "reply" ? "reply" : ""}`}>
              <div className="run-step-text">
                <strong>{s.action}</strong>
                <span className="task-meta">
                  {many && s.agent_name ? <span className="app-pill">{s.agent_name}</span> : null}
                  {s.tool ? <code className="job-trace-tool">{s.tool}</code> : null}
                  {s.ms ? <span className="faint">{took(s.ms)}</span> : null}
                  {s.attempt > 1 ? <span className="faint">try {s.attempt}</span> : null}
                </span>
                {s.kind === "tool" ? (
                  s.why ? (
                    <span className="job-trace-why">{s.why}</span>
                  ) : (
                    <span className="faint">It gave no reason before this step.</span>
                  )
                ) : null}
                {s.error ? <span className="err">{s.error}</span> : null}
                {s.kind === "reply" ? <span className="job-trace-why">{s.result}</span> : null}
                {s.kind === "tool" && Object.keys(s.args || {}).length ? (
                  <details>
                    <summary>What it sent</summary>
                    <pre className="job-pre">{JSON.stringify(s.args, null, 2)}</pre>
                  </details>
                ) : null}
                {s.kind === "tool" && s.result ? (
                  <details open={!s.ok}>
                    <summary>What came back</summary>
                    <pre className="job-pre">{pretty(s.result)}</pre>
                  </details>
                ) : null}
                {s.browser_run_id ? (
                  <a href={`/runs?run=${s.browser_run_id}`} target="_blank" rel="noreferrer">
                    Open the browser run step by step
                  </a>
                ) : null}
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
    </div>,
    document.body,
  );
}
