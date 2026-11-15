"use client";

import { useEffect, useMemo, useState } from "react";
import { AppShell } from "../components/AppShell";
import { AgentAvatar } from "../components/AgentAvatar";
import { AgentSelect, Select } from "../components/Select";
import {
  chatHref,
  getMe,
  loadWorkspace,
  acceptLesson,
  addLesson,
  dismissLesson,
  listLessons,
  removeLesson,
  qualityExport,
  qualityFeedback,
  qualitySummary,
  rateReply,
  relativeTime,
  type AgentLesson,
  type Me,
  type QualitySummary,
  type ReplyRating,
  type WorkspaceAgent,
} from "../lib";

// Quality (docs/QUALITY.md): how each agent is doing, from what you do: thumbs up / down on replies, routine runs
// that succeeded or failed, and how much of an inbox draft you changed before sending. Replies that went wrong can
// be exported as agent test cases.

const PERIODS = [7, 30, 90];

function pct(v: number | null | undefined) {
  return v === null || v === undefined ? "–" : `${v}%`;
}

function tone(v: number | null | undefined) {
  return v === null || v === undefined ? "neutral" : v >= 85 ? "good" : v >= 65 ? "warn" : "bad";
}

export default function QualityPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [days, setDays] = useState(30);
  const [agent, setAgent] = useState("");
  const [agents, setAgents] = useState<WorkspaceAgent[]>([]);
  const [summary, setSummary] = useState<QualitySummary | null>(null);
  const [bad, setBad] = useState<ReplyRating[] | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [proposed, setProposed] = useState<AgentLesson[]>([]);
  const [inUse, setInUse] = useState<AgentLesson[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [newFor, setNewFor] = useState("");
  const [newText, setNewText] = useState("");

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
      const wanted = new URLSearchParams(window.location.search).get("agent") || "";
      if (wanted) setAgent(wanted);
      loadWorkspace()
        .then((w) => {
          setAgents(w.agents || []);
          setNewFor(wanted || w.default_agent_id || w.agents?.[0]?.id || "");
        })
        .catch(() => undefined);
    });
  }, []);

  async function refresh() {
    try {
      const [s, f, p, a] = await Promise.all([
        qualitySummary(days, agent),
        qualityFeedback({ rating: -1, agentId: agent, days }),
        listLessons("proposed", agent),
        listLessons("accepted", agent),
      ]);
      setSummary(s);
      setBad(f.feedback);
      setProposed(p.lessons);
      setInUse(a.lessons);
      setError("");
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    if (me) void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me, days, agent]);

  async function exportCases(ids: string[] = []) {
    try {
      const data = await qualityExport(ids, Math.max(days, 90));
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = ids.length === 1 ? "lumio-test-case.json" : "lumio-test-cases.json";
      a.click();
      URL.revokeObjectURL(a.href);
      setNotice(`Exported ${data.cases.length} test case${data.cases.length === 1 ? "" : "s"}. Add them with: python -m evals.from_feedback <file>`);
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function lessonAct(fn: () => Promise<unknown>, done: string) {
    try {
      await fn();
      setNotice(done);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function clear(r: ReplyRating) {
    try {
      await rateReply({ session_id: r.session_id, request_id: r.request_id, rating: 0, agent_id: r.agent_id || "" });
      await refresh();
    } catch (e: any) {
      setError(e.message);
    }
  }

  const avatarOf = (id: string | null) => agents.find((a) => a.id === id);
  const s = summary;
  const reasonMax = useMemo(() => Math.max(1, ...(s?.reasons || []).map((r) => r.count)), [s]);
  const tiles = s
    ? [
        { label: "Replies rated good", value: pct(s.satisfaction), sub: `${s.up} good · ${s.down} not good`, t: tone(s.satisfaction) },
        { label: "Routine runs succeeded", value: pct(s.routines.success_rate), sub: `${s.routines.succeeded} ok · ${s.routines.failed} failed`, t: tone(s.routines.success_rate) },
        ...(agent
          ? []
          : [{ label: "Inbox drafts kept", value: pct(s.drafts.kept_rate), sub: `${s.drafts.sent} sent · ${s.drafts.rewritten} rewritten`, t: tone(s.drafts.kept_rate) }]),
        { label: "Replies that went wrong", value: String(s.down), sub: s.down ? "See below" : "None this period", t: s.down ? "bad" : "good" },
      ]
    : [];

  return (
    <AppShell me={me} current="quality">
      <div className="page">
        <div className="tasks-page">
          <div className="page-hero">
            <div>
              <span className="eyebrow">Profile</span>
              <h1>Quality</h1>
              <p className="muted">
                How your agents are doing, from what you do: the thumbs up or down you give replies, routine runs, and how much of an
                inbox draft you change before sending. Every thumbs-down can become a test your agents must pass.
              </p>
            </div>
            <div className="routines-hero-actions">
              <div className="skills-target">
                <span>Period</span>
                <Select value={String(days)} onChange={(v) => setDays(Number(v))} label="Period" options={PERIODS.map((d) => ({ value: String(d), label: `Last ${d} days` }))} />
              </div>
            </div>
          </div>

          {error ? <p className="err">{error}</p> : null}
          {notice ? <p className="notice">{notice}</p> : null}

          <section className="routine-tiles quality-tiles" aria-label="Quality at a glance">
            {tiles.map((t) => (
              <div key={t.label} className={`routine-tile tone-${t.t}`}>
                <strong>{t.value}</strong>
                <span>{t.label}</span>
                <span className="quality-sub">{t.sub}</span>
              </div>
            ))}
          </section>

          {proposed.length ? (
            <section className="lessons-review" aria-label="Lessons to review">
              <h2>
                Lessons to review <span className="pill-count">{proposed.length}</span>
              </h2>
              <p className="muted">
                From replies you marked wrong. Accept one and that agent follows it from its next reply; edit it first if you like.
              </p>
              {proposed.map((l) => {
                const row = avatarOf(l.agent_id);
                return (
                  <article key={l.id} className="lesson-card">
                    <header>
                      <AgentAvatar name={l.agent_name} color={l.agent_color || row?.color} avatar={row?.avatar} className="agent-avatar tiny" />
                      <strong>{l.agent_name}</strong>
                      <span className="faint">will remember</span>
                    </header>
                    <textarea
                      aria-label={`Lesson for ${l.agent_name}`}
                      rows={2}
                      maxLength={240}
                      value={drafts[l.id] ?? l.text}
                      onChange={(e) => setDrafts((d) => ({ ...d, [l.id]: e.target.value }))}
                    />
                    {l.why ? <p className="faint lesson-why">Because: {l.why}</p> : null}
                    <div className="row">
                      <button className="btn small" type="button" onClick={() => lessonAct(() => acceptLesson(l.id, drafts[l.id] ?? ""), `${l.agent_name} will follow this from now on.`)}>
                        Accept
                      </button>
                      <button className="btn secondary small" type="button" onClick={() => lessonAct(() => dismissLesson(l.id), "Dismissed.")}>
                        Dismiss
                      </button>
                    </div>
                  </article>
                );
              })}
            </section>
          ) : null}

          {s && s.agents.length ? (
            <div className="routine-agents" role="tablist" aria-label="Agent">
              <button type="button" role="tab" aria-selected={!agent} className={`routine-agent${!agent ? " on" : ""}`} onClick={() => setAgent("")}>
                <strong>All agents</strong>
              </button>
              {s.agents.map((a) => {
                const row = avatarOf(a.agent_id);
                return (
                  <button key={a.agent_id || "none"} type="button" role="tab" aria-selected={agent === a.agent_id} className={`routine-agent${agent === a.agent_id ? " on" : ""}`} onClick={() => setAgent(a.agent_id || "")}>
                    <AgentAvatar name={a.agent_name} color={a.agent_color || row?.color} avatar={row?.avatar} className="agent-avatar tiny" />
                    <span className="routine-agent-copy">
                      <strong>{a.agent_name}</strong>
                      <span>
                        {a.satisfaction === null ? "Not rated" : <span className={a.satisfaction >= 85 ? "good" : a.satisfaction >= 65 ? "" : "bad"}>{a.satisfaction}% good</span>}
                        {a.routine_failed ? <> · <span className="bad">{a.routine_failed} run{a.routine_failed === 1 ? "" : "s"} failed</span></> : null}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          ) : null}

          {s?.reasons.length ? (
            <section className="quality-reasons" aria-label="Why replies went wrong">
              <h2>Why replies went wrong</h2>
              {s.reasons.map((r) => (
                <div key={r.reason} className="quality-reason">
                  <span>{r.label}</span>
                  <span className="quality-bar" aria-hidden>
                    <span style={{ width: `${(100 * r.count) / reasonMax}%` }} />
                  </span>
                  <strong>{r.count}</strong>
                </div>
              ))}
            </section>
          ) : null}

          <section className="lessons-in-use" aria-label="Lessons in use">
            <div className="vault-list-head">
              <h2>
                Lessons in use <span className="pill-count">{inUse.length}</span>
              </h2>
            </div>
            {inUse.length ? (
              <div className="vault-list">
                {inUse.map((l) => {
                  const row = avatarOf(l.agent_id);
                  return (
                    <div key={l.id} className="lesson-row">
                      <AgentAvatar name={l.agent_name} color={l.agent_color || row?.color} avatar={row?.avatar} className="agent-avatar tiny" />
                      <span className="lesson-text">
                        <strong>{l.agent_name}</strong> {l.text}
                      </span>
                      <span className="faint lesson-src">{l.source === "manual" ? "Added by you" : "From feedback"}</span>
                      <button className="btn secondary small" type="button" onClick={() => lessonAct(() => removeLesson(l.id), "Removed: the agent won't follow it any more.")}>
                        Remove
                      </button>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="muted">No lessons yet. They come from replies you mark wrong, or add one yourself.</p>
            )}
            <form
              className="lesson-add"
              onSubmit={(e) => {
                e.preventDefault();
                if (!newText.trim() || !newFor) return;
                void lessonAct(async () => {
                  await addLesson(newFor, newText);
                  setNewText("");
                }, "Added. The agent follows it from its next reply.");
              }}
            >
              <div className="lesson-add-agent">
                <AgentSelect value={newFor} onChange={setNewFor} label="Agent" agents={agents.filter((a) => !a.archived)} />
              </div>
              <input aria-label="New lesson" placeholder="e.g. Always answer amounts in INR" value={newText} maxLength={240} onChange={(e) => setNewText(e.target.value)} />
              <button className="btn secondary" disabled={!newText.trim() || !newFor}>
                Add lesson
              </button>
            </form>
          </section>

          <div className="vault-list-head">
            <h2>
              Replies that went wrong <span className="pill-count">{bad?.length ?? 0}</span>
            </h2>
            {bad?.length ? (
              <button className="btn secondary small" type="button" onClick={() => exportCases()}>
                Export as test cases
              </button>
            ) : null}
          </div>

          {bad === null ? <p className="muted">Loading…</p> : null}
          {bad && bad.length === 0 ? (
            <div className="task-empty">
              <p className="muted">
                Nothing marked wrong in the last {days} days. When a reply misses, press the thumbs-down under it and say what went wrong;
                it shows up here.
              </p>
            </div>
          ) : null}
          <div className="quality-list">
            {(bad || []).map((r) => {
              const row = avatarOf(r.agent_id);
              return (
                <article key={r.id} className="quality-item">
                  <header>
                    <AgentAvatar name={r.agent_name || "Agent"} color={r.agent_color || row?.color} avatar={row?.avatar} className="agent-avatar tiny" />
                    <strong>{r.agent_name || "Agent"}</strong>
                    <span className="faint">{relativeTime(r.at)}</span>
                    <span className="quality-chips">
                      {r.reason_labels.map((l) => (
                        <span key={l} className="run-chip st-failed">
                          {l}
                        </span>
                      ))}
                    </span>
                  </header>
                  {r.note ? <p className="quality-note">“{r.note}”</p> : null}
                  <div className="quality-exchange">
                    <div>
                      <span className="faint">You asked</span>
                      <p>{r.prompt || "—"}</p>
                    </div>
                    <div>
                      <span className="faint">It replied</span>
                      <p className="quality-reply">{r.reply}</p>
                    </div>
                  </div>
                  {r.tools.length ? (
                    <div className="quality-tools">
                      {r.tools.slice(0, 8).map((t) => (
                        <code key={t}>{t}</code>
                      ))}
                    </div>
                  ) : null}
                  <footer className="row">
                    <a className="btn secondary small" href={chatHref({ agent: r.agent_id, conversation: r.session_id })}>
                      Open conversation
                    </a>
                    <button className="btn secondary small" type="button" onClick={() => exportCases([r.id])}>
                      Export as test case
                    </button>
                    <span style={{ flex: 1 }} />
                    <button className="btn secondary small" type="button" onClick={() => clear(r)}>
                      Clear rating
                    </button>
                  </footer>
                </article>
              );
            })}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
