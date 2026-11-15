"use client";

import { useEffect, useMemo, useState } from "react";
import { AppShell } from "../components/AppShell";
import { AgentAvatar } from "../components/AgentAvatar";
import {
  answerApproval,
  approveChatApproval,
  chatHref,
  decideApprovals,
  dismissChatApproval,
  getMe,
  listApprovals,
  relativeTime,
  type ApprovalItem,
  type ApprovalKind,
  type ApprovalQueue,
  type Me,
} from "../lib";

// Approvals (docs/APPROVALS.md): everything any agent is waiting on you for, in one place. Plans and paused actions of
// background tasks can be decided in a batch; chat approvals are approved through their chat, like typing "yes".

const KIND_LABEL: Record<ApprovalKind, string> = {
  plan: "Plan to start",
  action: "Needs your OK",
  question: "Question",
  handoff: "Your turn on the computer",
  chat: "Needs your OK · in chat",
};
const FILTERS: { key: "all" | ApprovalKind; label: string }[] = [
  { key: "all", label: "All" },
  { key: "plan", label: "Plans" },
  { key: "action", label: "Actions" },
  { key: "chat", label: "In chat" },
  { key: "question", label: "Questions" },
];
const batchable = (i: ApprovalItem) => (i.kind === "plan" || (i.kind === "action" && !i.expired)) && !!i.job_id;

function minutesLeft(exp?: number | null) {
  if (!exp) return "";
  const m = Math.round((exp * 1000 - Date.now()) / 60000);
  return m <= 0 ? "expiring now" : m < 60 ? `expires in ${m} min` : "";
}

export default function ApprovalsPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [queue, setQueue] = useState<ApprovalQueue | null>(null);
  const [filter, setFilter] = useState<"all" | ApprovalKind>("all");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function refresh() {
    try {
      const next = await listApprovals();
      setQueue(next);
      setPicked((p) => new Set([...p].filter((id) => next.items.some((i) => i.id === id && batchable(i)))));
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    getMe().then(setMe).catch(() => setMe(null));
    void refresh();
    const t = window.setInterval(() => void refresh(), 20000);
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(t);
      window.removeEventListener("focus", onFocus);
    };
  }, []);

  const matches = (i: ApprovalItem, f: "all" | ApprovalKind) => f === "all" || i.kind === f || (f === "chat" && i.kind === "handoff");
  const items = useMemo(() => (queue?.items || []).filter((i) => matches(i, filter)), [queue, filter]);
  const pickable = items.filter(batchable);
  const chosen = pickable.filter((i) => picked.has(i.id));

  async function decide(list: ApprovalItem[], approve: boolean) {
    if (!list.length) return;
    setBusy(approve ? "approve" : "decline");
    setError("");
    setNotice("");
    try {
      const out = await decideApprovals(list.map((i) => ({ id: i.id, kind: i.kind, job_id: i.job_id as string, token: i.token, approve })));
      const failed = out.results.filter((r) => !r.ok);
      setNotice(
        `${out.done} ${approve ? "approved" : "declined"}` + (failed.length ? ` · ${failed.length} couldn’t be: ${failed[0].error}` : "") + (approve && out.done ? ". They carry on in the background." : "."),
      );
      setPicked(new Set());
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function one(item: ApprovalItem, key: string, fn: () => Promise<unknown>, done: string) {
    setBusy(`${key}:${item.id}`);
    setError("");
    setNotice("");
    try {
      await fn();
      setNotice(done);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  const toggle = (id: string) => setPicked((p) => {
    const n = new Set(p);
    n.has(id) ? n.delete(id) : n.add(id);
    return n;
  });

  return (
    <AppShell me={me} current="approvals">
      <div className="page">
        <div className="tasks-page approvals-page">
          <div className="page-hero">
            <div>
              <span className="eyebrow">Workspace</span>
              <h1>Approvals</h1>
              <p className="muted">
                Everything your agents are waiting on you for, across every agent: plans to start, actions that need your OK, and questions.
                Approve several at once, or open the chat for context.
              </p>
            </div>
          </div>

          <div className="approvals-filters" role="tablist" aria-label="Show">
            {FILTERS.map((f) => {
              const n = (queue?.items || []).filter((i) => matches(i, f.key)).length;
              return (
                <button key={f.key} type="button" role="tab" aria-selected={filter === f.key} className={`pill${filter === f.key ? " on" : ""}`} onClick={() => setFilter(f.key)}>
                  {f.label}
                  {n ? <span className="pill-count">{n}</span> : null}
                </button>
              );
            })}
          </div>

          {pickable.length ? (
            <div className="approvals-batch">
              <label className="approvals-check">
                <input
                  type="checkbox"
                  checked={chosen.length === pickable.length}
                  ref={(el) => {
                    if (el) el.indeterminate = chosen.length > 0 && chosen.length < pickable.length;
                  }}
                  onChange={() => setPicked(chosen.length === pickable.length ? new Set() : new Set(pickable.map((i) => i.id)))}
                />
                <span>{chosen.length ? `${chosen.length} selected` : `Select all ${pickable.length}`}</span>
              </label>
              <div className="row">
                <button className="btn small" type="button" disabled={!chosen.length || !!busy} onClick={() => decide(chosen, true)}>
                  {busy === "approve" ? "Approving…" : `Approve${chosen.length ? ` ${chosen.length}` : ""}`}
                </button>
                <button className="btn secondary small" type="button" disabled={!chosen.length || !!busy} onClick={() => decide(chosen, false)}>
                  {busy === "decline" ? "Declining…" : "Decline"}
                </button>
              </div>
            </div>
          ) : null}

          {error ? <p className="err">{error}</p> : null}
          {notice ? <p className="approvals-notice">{notice}</p> : null}
          {queue === null ? <p className="muted">Loading…</p> : null}
          {queue && !items.length ? (
            <div className="approvals-empty">
              <strong>Nothing waiting on you</strong>
              <span className="muted">When an agent needs your OK, a plan started or an answer, it shows up here.</span>
            </div>
          ) : null}

          <div className="approvals-list">
            {items.map((i) => {
              const href = i.agent_id ? chatHref({ agent: i.agent_id, conversation: i.session_id }) : "/chat";
              const can = batchable(i);
              const b = (k: string) => busy === `${k}:${i.id}`;
              return (
                <article key={i.id} className={`approval-card kind-${i.kind}${picked.has(i.id) ? " picked" : ""}`}>
                  {can ? (
                    <input type="checkbox" className="approval-pick" aria-label={`Select ${i.title}`} checked={picked.has(i.id)} onChange={() => toggle(i.id)} />
                  ) : (
                    <span className="approval-pick" aria-hidden />
                  )}
                  <div className="approval-main">
                    <header>
                      <AgentAvatar name={i.agent_name || "Agent"} color={i.agent_color} avatar={i.agent_avatar} className="agent-avatar tiny" />
                      <strong>{i.agent_name || "Agent"}</strong>
                      <span className={`approval-kind ${i.kind}`}>{KIND_LABEL[i.kind]}</span>
                      <span className="faint approval-when">
                        {i.since ? relativeTime(i.since) : ""}
                        {i.kind === "chat" && minutesLeft(i.expires_at) ? ` · ${minutesLeft(i.expires_at)}` : ""}
                      </span>
                    </header>
                    <h3>{i.title}</h3>
                    {i.job_title && i.kind !== "plan" ? <p className="faint approval-task">Task: {i.job_title}</p> : null}
                    {i.lines.length ? (
                      i.kind === "plan" ? (
                        <ol className="approval-lines steps">
                          {i.lines.map((l, n) => (
                            <li key={n}>{l}</li>
                          ))}
                        </ol>
                      ) : (
                        <ul className="approval-lines">
                          {i.lines.map((l, n) => (
                            <li key={n}>{l}</li>
                          ))}
                        </ul>
                      )
                    ) : null}
                    {i.expired ? <p className="faint">This approval expired. Ask the agent to prepare it again in the chat.</p> : null}
                    <div className="approval-actions row">
                      {i.kind === "plan" ? (
                        <>
                          <button className="btn small" type="button" disabled={!!busy} onClick={() => decide([i], true)}>
                            Start
                          </button>
                          <button className="btn secondary small" type="button" disabled={!!busy} onClick={() => decide([i], false)}>
                            Don’t run it
                          </button>
                        </>
                      ) : null}
                      {i.kind === "action" && !i.expired ? (
                        <>
                          <button className="btn small" type="button" disabled={!!busy} onClick={() => decide([i], true)}>
                            Approve
                          </button>
                          <button className="btn secondary small" type="button" disabled={!!busy} onClick={() => decide([i], false)}>
                            Decline
                          </button>
                        </>
                      ) : null}
                      {i.kind === "chat" ? (
                        <>
                          <button
                            className="btn small"
                            type="button"
                            disabled={!!busy}
                            onClick={() => one(i, "approve", () => approveChatApproval(i), `Approved. ${i.agent_name || "The agent"} is doing it now; the result is in the chat.`)}
                          >
                            {b("approve") ? "Running…" : "Approve"}
                          </button>
                          <button
                            className="btn secondary small"
                            type="button"
                            disabled={!!busy}
                            onClick={() => one(i, "decline", () => dismissChatApproval(i.message_id as number, i.token as string), "Declined. It won’t run.")}
                          >
                            Decline
                          </button>
                        </>
                      ) : null}
                      {i.kind === "question" && i.options?.length
                        ? i.options.map((o) => (
                            <button
                              key={o}
                              className="btn secondary small"
                              type="button"
                              disabled={!!busy}
                              onClick={() => one(i, "answer", () => answerApproval(i.job_id as string, o), `Answered “${o}”.`)}
                            >
                              {o}
                            </button>
                          ))
                        : null}
                      <a className="btn secondary small" href={href}>
                        {i.kind === "handoff" ? "Finish in the chat" : i.kind === "question" && !i.options?.length ? "Answer in the chat" : "Open chat"}
                      </a>
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
