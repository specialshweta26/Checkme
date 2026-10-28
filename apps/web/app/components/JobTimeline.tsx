"use client";

/**
 * Live timeline for a background agent job (and the jobs it delegated to).
 *
 * Polls `GET /api/jobs/{id}/events?after=` (the same short-poll pattern as chat progress)
 * and renders plan / step / test / diff / PR / approval / result entries. Approval entries
 * get Approve and Decline buttons that call `POST /api/jobs/{id}/approve|decline`; nothing
 * is pushed, opened, merged or deployed until the user approves. Questions (e.g. "Merge it?")
 * get option buttons that call `POST /api/jobs/{id}/answer`. The latest write_todos plan is
 * shown as a checklist, and check runs show their results table.
 */
import { useEffect, useRef, useState } from "react";
import { formatPreview } from "./preview";

type JobRow = {
  id: string;
  parent_id: string | null;
  agent_name: string;
  role: string;
  title: string;
  status: string;
  error?: string;
  pending: { token: string; title: string; preview: any; kind?: string }[];
  question?: { question: string; options: string[] } | null;
};
type Todo = { content: string; status: "pending" | "in_progress" | "completed" };
type JobEvent = { id: number; job_id: string; agent_name: string; kind: string; text: string; data: any; at: string | null };

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  running: "Working",
  waiting_approval: "Needs your approval",
  waiting_children: "Delegated",
  waiting_input: "Needs your answer",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};
const ICON: Record<string, string> = {
  step: "•",
  plan: "☰",
  test: "✓",
  diff: "±",
  pr: "↗",
  message: "💬",
  approval: "⏸",
  result: "★",
  error: "!",
  status: "·",
  question: "?",
  deploy: "🚀",
  login: "🔑",
};
const TODO_MARK: Record<string, string> = { completed: "✓", in_progress: "▸", pending: "○" };

function skillStep(st: { step_index?: number; step_count?: number }) {
  const done = st.step_index || 0;
  const total = st.step_count || 0;
  if (!total) return `step ${done + 1}`;
  return done >= total ? `all ${total} steps done` : `step ${done + 1}/${total}`;
}

async function post(path: string, body?: any) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request failed");
  return data;
}

export function JobTimeline({ jobId, after: startAfter = 0, onSettled }: { jobId: string; after?: number; onSettled?: () => void }) {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [nudge, setNudge] = useState(0);
  const [loginCode, setLoginCode] = useState("");
  const after = useRef(startAfter);
  const settled = useRef(false);
  const sawActive = useRef(false);

  useEffect(() => {
    let stopped = false;
    let timer: number | null = null;
    const tick = async () => {
      let waiting = false;
      let finished = false;
      try {
        const res = await fetch(`/api/jobs/${jobId}/events?after=${after.current}`, { credentials: "include", cache: "no-store" });
        if (res.ok) {
          const data = await res.json();
          const fresh: JobEvent[] = data.events || [];
          if (fresh.length) {
            after.current = fresh[fresh.length - 1].id;
            if (!stopped) setEvents((all) => [...all, ...fresh].slice(-400));
          }
          if (!stopped) setJobs(data.jobs || []);
          waiting = (data.waiting || []).length > 0;
          finished = Boolean(data.done);
          if (!stopped) setDone(finished);
          if (!finished) sawActive.current = true;
          if (finished && sawActive.current && !settled.current) {
            // Only when we watched it finish: the job's final reply was just posted to the chat.
            settled.current = true;
            onSettled?.();
          }
        }
      } catch {
        // Best effort; try again on the next tick.
      }
      if (!stopped && !finished) timer = window.setTimeout(tick, waiting ? 6000 : 2500);
    };
    tick();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [jobId, nudge]);

  const byId = Object.fromEntries(jobs.map((j) => [j.id, j]));
  const root = jobs.find((j) => j.id === jobId) || jobs.find((j) => !j.parent_id);
  const status = root?.status || "queued";

  async function decide(job: string, token: string, approve: boolean) {
    setBusy(token);
    setErr("");
    try {
      await post(`/api/jobs/${job}/${approve ? "approve" : "decline"}`, { token });
      setNudge((n) => n + 1);
    } catch (e: any) {
      setErr(e.message || "Could not update the job");
    } finally {
      setBusy("");
    }
  }

  async function reply(job: string, text: string) {
    setBusy(`answer:${job}`);
    setErr("");
    try {
      await post(`/api/jobs/${job}/answer`, { text });
      setNudge((n) => n + 1);
    } catch (e: any) {
      setErr(e.message || "Could not send the answer");
    } finally {
      setBusy("");
    }
  }

  const planEvent = [...events].reverse().find((ev) => ev.kind === "plan" && Array.isArray(ev.data?.todos) && ev.data.todos.length);
  const todos: Todo[] = planEvent ? planEvent.data.todos : [];
  // Latest skill state per agent in this job tree: which skill each task follows and its step.
  const skillStates = Object.values(
    events.reduce<Record<string, any>>((acc, ev) => {
      const st = ev.data?.skill_state;
      if (st && st.id) acc[st.id] = { ...st, who: ev.agent_name };
      return acc;
    }, {}),
  ).filter((st: any) => st.status !== "dropped");

  async function cancel() {
    setBusy("cancel");
    try {
      await post(`/api/jobs/${root?.id || jobId}/cancel`);
      setNudge((n) => n + 1);
    } catch (e: any) {
      setErr(e.message || "Could not cancel");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="job-timeline" aria-live="polite">
      <div className="job-head">
        <span className={`job-badge ${status}`}>{STATUS_LABEL[status] || status}</span>
        <strong className="job-title">{root?.title || "Background job"}</strong>
        {!done ? (
          <button className="btn secondary small" type="button" disabled={busy === "cancel"} onClick={cancel}>
            Cancel
          </button>
        ) : null}
      </div>
      {jobs.length > 1 ? (
        <div className="job-tree">
          {jobs.map((j) => (
            <span key={j.id} className={`job-chip ${j.status}`}>
              {j.agent_name || j.role} · {j.role} · {STATUS_LABEL[j.status] || j.status}
            </span>
          ))}
        </div>
      ) : null}
      {skillStates.length ? (
        <div className="job-tree job-skills" aria-label="Skills in use">
          {skillStates.map((st: any) => (
            <span key={st.id} className={`job-chip skill ${st.status}`} title={(st.notes || []).map((n: any) => n.text).join("\n")}>
              {st.who ? `${st.who} · ` : ""}Skill: {st.skill_name} · {skillStep(st)}
              {st.status === "handed_off" ? " · handed over" : st.status === "done" ? " · done" : ""}
            </span>
          ))}
        </div>
      ) : null}
      {todos.length ? (
        <ul className="job-plan" aria-label="Plan">
          {todos.map((t, i) => (
            <li key={i} className={`job-todo ${t.status}`}>
              <span className="job-todo-mark">{TODO_MARK[t.status] || "○"}</span>
              <span>{t.content}</span>
            </li>
          ))}
        </ul>
      ) : null}
      <ol className="job-events">
        {events.map((ev) => {
          const owner = byId[ev.job_id];
          const stillPending = ev.kind === "approval" && owner?.status === "waiting_approval" && owner.pending.some((p) => p.token === ev.data?.token);
          const openQuestion = ev.kind === "question" && owner?.status === "waiting_input" && owner.question?.question === ev.text;
          return (
            <li key={ev.id} className={`job-ev ${ev.kind}`}>
              <span className="job-ico">{ICON[ev.kind] || "•"}</span>
              <div className="job-body">
                {ev.agent_name && jobs.length > 1 ? <span className="job-who">{ev.agent_name}</span> : null}
                {(ev.kind === "pr" || ev.kind === "deploy") && ev.data?.url ? (
                  <a href={ev.data.url} target="_blank" rel="noreferrer">
                    {ev.text}
                  </a>
                ) : (
                  <span className="job-text">{ev.text}</span>
                )}
                {ev.kind === "login" && ev.data?.url ? (
                  <div className="job-login">
                    <a className="btn secondary" href={ev.data.url} target="_blank" rel="noreferrer">
                      Open sign-in page
                    </a>
                    {ev.data?.code && !ev.data?.needs_code ? (
                      <span className="job-text">
                        {" "}
                        Code: <code>{ev.data.code}</code>
                      </span>
                    ) : null}
                    {ev.data?.needs_code && owner?.status === "waiting_input" ? (
                      <form
                        className="approval-actions"
                        onSubmit={(e) => {
                          e.preventDefault();
                          const code = loginCode.trim();
                          if (!code) return;
                          setLoginCode("");
                          reply(ev.job_id, code);
                        }}
                      >
                        <input
                          className="input"
                          type="password"
                          autoComplete="off"
                          placeholder="Paste the code the page shows"
                          value={loginCode}
                          onChange={(e) => setLoginCode(e.target.value)}
                        />
                        <button className="btn" type="submit" disabled={!!busy || !loginCode.trim()}>
                          Send code
                        </button>
                      </form>
                    ) : null}
                  </div>
                ) : null}
                {ev.kind === "test" && ev.data?.table ? (
                  <details open={ev.data?.passed === false}>
                    <summary>Check results{ev.data?.attempt ? ` (run ${ev.data.attempt})` : ""}</summary>
                    <pre className="job-pre">{ev.data.table}</pre>
                  </details>
                ) : null}
                {openQuestion ? (
                  <div className="approval-actions job-choices">
                    {(owner?.question?.options || []).map((opt) => (
                      <button key={opt} className="btn secondary" type="button" disabled={!!busy} onClick={() => reply(ev.job_id, opt)}>
                        {opt}
                      </button>
                    ))}
                  </div>
                ) : null}
                {(ev.kind === "diff" || ev.kind === "test" || ev.kind === "deploy") && ev.data?.output ? (
                  <details>
                    <summary>{ev.kind === "diff" ? "Show diff" : "Show output"}</summary>
                    <pre className="job-pre">{ev.data.output}</pre>
                  </details>
                ) : null}
                {ev.kind === "approval" ? (
                  <div className="job-approval">
                    <pre className="approval-preview">{formatPreview(ev.data?.preview)}</pre>
                    {stillPending ? (
                      <div className="approval-actions">
                        <button className="btn" type="button" disabled={!!busy} onClick={() => decide(ev.job_id, ev.data.token, true)}>
                          Approve
                        </button>
                        <button className="btn secondary" type="button" disabled={!!busy} onClick={() => decide(ev.job_id, ev.data.token, false)}>
                          Decline
                        </button>
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </div>
            </li>
          );
        })}
        {!done && status !== "waiting_approval" && status !== "waiting_input" ? (
          <li className="job-ev live">
            <span className="think-orb" />
            <span className="job-text muted">{status === "queued" ? "Waiting for the computer…" : "Working…"}</span>
          </li>
        ) : null}
      </ol>
      {err ? <p className="err">{err}</p> : null}
    </div>
  );
}
