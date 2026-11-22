"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AppShell } from "../components/AppShell";
import { apiError, chatHref, getMe, type Me } from "../lib";

// The admin console (apps/api/app/admin/routes.py, docs/ADMIN.md): people, today's use, invites, suspension, audit log.

type Overview = {
  users: number;
  new_7d: number;
  suspended: number;
  active_today: number;
  tokens_today: number;
  signup_mode: "open" | "invite" | "closed";
  signup_allow_env: string[];
  cap: { tokens: number | null; usd: number | null };
};
type Person = {
  id: string;
  email: string;
  name: string;
  provider: string;
  created_at: string | null;
  last_sign_in: string | null;
  suspended: boolean;
  admin: boolean;
  exempt: boolean;
  over_cap: boolean;
  today: { tokens?: number; cost_usd?: number };
};
type Invite = { id: string; value: string; note: string; created_at: string };
type Audit = { at: string; action: string; user: string; actor: string; target: string; ip: string };
type SlowCall = { connector?: string; tool?: string; ms?: number; first_ms?: number; prep_ms?: number; pages?: number; cached?: boolean };
type Reply = {
  at: string;
  user: string;
  total_ms: number;
  model_ms: number;
  model_calls: number;
  connector_ms: number;
  tool_ms: number;
  slowest_calls: SlowCall[];
  computer_used: boolean;
  ended: string;
  tools?: { tool: string; calls: number; ms: number }[];
  other_ms?: number;
  prep_ms?: number;
  stages?: Record<string, number>;
};
type Latency = {
  target_s: number;
  replies: number;
  over_target: number;
  p50_ms: number | null;
  p95_ms: number | null;
  max_ms: number | null;
  over_target_split: { model_ms: number; connector_ms: number; tool_ms: number };
  slowest: Reply[];
};

type Scenario = { id: string; group: string; text: string; after?: string };
type ScenarioResult = {
  id: string;
  group: string;
  text: string;
  status: "waiting" | "running" | "ok" | "error" | "timeout" | "skipped";
  total_ms?: number;
  model_ms?: number;
  model_calls?: number;
  connector_ms?: number;
  tool_ms?: number;
  other_ms?: number;
  over_target?: boolean;
  waiting_approval?: boolean;
  reply?: string;
  error?: string;
  session_id?: string;
  agent_id?: string;
  tools?: { tool: string; calls: number; ms: number }[];
};
type ScenarioRun = { id: string; status: string; target_s: number; started_at: string; results: ScenarioResult[] };

const fmt = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n));
const secs = (ms: number | null | undefined) => (ms == null ? "–" : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`);
const when = (s: string | null) => (s ? new Date(s).toLocaleString() : "never");

async function call(path: string, init?: RequestInit) {
  const res = await fetch(path, { credentials: "include", ...init, headers: { "Content-Type": "application/json", ...(init?.headers || {}) } });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

export default function AdminPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [denied, setDenied] = useState(false);
  const [ov, setOv] = useState<Overview | null>(null);
  const [people, setPeople] = useState<Person[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [lat, setLat] = useState<Latency | null>(null);
  const [latDays, setLatDays] = useState(1);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [custom, setCustom] = useState("");
  const [run, setRun] = useState<ScenarioRun | null>(null);
  const [q, setQ] = useState("");
  const [value, setValue] = useState("");
  const [send, setSend] = useState(true);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async (search = "") => {
    const o = await call("/api/admin/overview");
    if (o.status === 404 || o.status === 401) {
      setDenied(true);
      return;
    }
    setOv(o.data);
    const [p, i, a] = await Promise.all([
      call(`/api/admin/users?q=${encodeURIComponent(search)}`),
      call("/api/admin/invites"),
      call("/api/admin/audit?days=7"),
    ]);
    if (p.ok) setPeople(p.data);
    if (i.ok) setInvites(i.data);
    if (a.ok) setAudit(a.data.slice(0, 50));
    const failed = [p.ok ? "" : "people", i.ok ? "" : "invites", a.ok ? "" : "audit log"].filter(Boolean);
    setError(failed.length ? `Couldn't load the ${failed.join(", ")}. Reload the page to try again.` : "");
  }, []);

  const loadLatency = useCallback(async (days: number) => {
    const r = await call(`/api/admin/latency?days=${days}`);
    if (r.ok) setLat(r.data);
  }, []);

  useEffect(() => {
    if (me) loadLatency(latDays);
  }, [me, latDays, loadLatency]);

  const loadScenarios = useCallback(async (first = false) => {
    const r = await call("/api/admin/scenarios");
    if (!r.ok) return;
    if (first) {
      setScenarios(r.data.scenarios);
      setPicked(new Set(r.data.scenarios.map((x: Scenario) => x.id)));
    }
    setRun(r.data.run);
  }, []);

  useEffect(() => {
    if (me) loadScenarios(true);
  }, [me, loadScenarios]);

  useEffect(() => {
    if (run?.status !== "running") return;
    const t = window.setInterval(() => loadScenarios(), 3000);
    return () => window.clearInterval(t);
  }, [run?.status, loadScenarios]);

  async function runScenarios() {
    const own = custom.split("\n").map((x) => x.trim()).filter(Boolean);
    const r = await call("/api/admin/scenarios/run", { method: "POST", body: JSON.stringify({ ids: [...picked], custom: own }) });
    if (r.ok) setRun(r.data);
    else setError(apiError(r.data, "The test run couldn't start."));
  }

  async function stopScenarios() {
    const r = await call("/api/admin/scenarios/stop", { method: "POST" });
    if (r.ok && r.data.results) setRun(r.data);
  }

  function toggle(id: string) {
    setPicked((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
      load();
    });
  }, [load]);

  async function addInvite(e: FormEvent) {
    e.preventDefault();
    setMsg("");
    setError("");
    const r = await call("/api/admin/invites", { method: "POST", body: JSON.stringify({ value, send }) });
    if (!r.ok) {
      setError(apiError(r.data, "The invite couldn't be added."));
      return;
    }
    setValue("");
    setMsg(r.data.sent ? `Invited ${r.data.value} and emailed them.` : `Added ${r.data.value}.`);
    load(q);
  }

  async function removeInvite(id: string) {
    await call(`/api/admin/invites/${id}`, { method: "DELETE" });
    load(q);
  }

  async function setSuspended(p: Person, suspended: boolean) {
    if (suspended && !window.confirm(`Suspend ${p.email}? They're signed out everywhere and their tasks stop.`)) return;
    const r = await call(`/api/admin/users/${p.id}/suspend`, { method: "POST", body: JSON.stringify({ suspended }) });
    if (!r.ok) setError(apiError(r.data, "That didn't work."));
    load(q);
  }

  if (denied) {
    return (
      <AppShell me={me} current="admin">
        <div className="page">
          <div className="tasks-page">
            <h1>Admin console</h1>
            <p className="muted">This page is for Lumio admins. Ask the team to add your email to ADMIN_EMAILS.</p>
          </div>
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell me={me} current="admin">
      <div className="page">
        <div className="tasks-page">
          <div className="page-hero">
            <div>
              <h1>Admin console</h1>
              <p className="muted">People, today&apos;s use, invites, reply times and the audit log.</p>
            </div>
          </div>

          {ov ? (
            <div className="admin-stats">
              <div className="task-card"><strong>{ov.users}</strong><span className="muted">people ({ov.new_7d} new this week)</span></div>
              <div className="task-card"><strong>{ov.active_today}</strong><span className="muted">active today</span></div>
              <div className="task-card"><strong>{fmt(ov.tokens_today)}</strong><span className="muted">tokens today</span></div>
              <div className="task-card">
                <strong>{ov.signup_mode}</strong>
                <span className="muted">
                  sign-ups · cap {ov.cap.tokens ? `${fmt(ov.cap.tokens)} tokens` : ov.cap.usd ? `$${ov.cap.usd}` : "none"}/day
                </span>
              </div>
            </div>
          ) : null}
          {error ? <p className="err">{error}</p> : null}
          {msg ? <p className="muted">{msg}</p> : null}

          <div className="task-card stack">
            <strong>Invites</strong>
            <span className="muted">
              {ov?.signup_mode === "invite"
                ? "Only these emails and domains can create an account."
                : ov?.signup_mode === "closed"
                  ? "Sign-ups are closed (SIGNUP_MODE=closed), so invites don't let anyone in yet."
                  : "Sign-ups are open to everyone right now (SIGNUP_MODE=open); invites apply once it's invite."}
            </span>
            <form className="row" onSubmit={addInvite}>
              <input className="field" placeholder="asha@gmail.com or acme.com" value={value} onChange={(e) => setValue(e.target.value)} />
              <label className="faint row">
                <input type="checkbox" checked={send} onChange={(e) => setSend(e.target.checked)} /> email them
              </label>
              <button className="btn" type="submit" disabled={!value.trim()}>Invite</button>
            </form>
            <ul className="admin-list">
              {ov?.signup_allow_env.map((v) => (
                <li key={`env-${v}`}><span>{v.trim()}</span><span className="faint">set in SIGNUP_ALLOW</span></li>
              ))}
              {invites.map((i) => (
                <li key={i.id}>
                  <span>{i.value}</span>
                  <button className="btn secondary small" onClick={() => removeInvite(i.id)}>Remove</button>
                </li>
              ))}
            </ul>
          </div>

          <div className="task-card stack">
            <div className="row spread">
              <strong>People</strong>
              <input className="field" placeholder="Search email or name" value={q} onChange={(e) => { setQ(e.target.value); load(e.target.value); }} />
            </div>
            <div className="admin-table" role="table">
              {people.map((p) => (
                <div className="admin-row" role="row" key={p.id}>
                  <div className="stack">
                    <span>{p.name || p.email} {p.admin ? <span className="faint">· admin</span> : null}</span>
                    <span className="faint">{p.email} · {p.provider} · joined {when(p.created_at)} · last sign-in {when(p.last_sign_in)}</span>
                  </div>
                  <span className={p.over_cap ? "err" : "muted"}>
                    {fmt(p.today.tokens || 0)} tokens{p.today.cost_usd ? ` · $${p.today.cost_usd.toFixed(2)}` : ""}
                    {p.exempt ? " · no cap" : p.over_cap ? " · at cap" : ""}
                  </span>
                  {p.suspended ? (
                    <button className="btn secondary small" onClick={() => setSuspended(p, false)}>Restore</button>
                  ) : (
                    <button className="btn danger small" disabled={p.id === me?.user_id} onClick={() => setSuspended(p, true)}>Suspend</button>
                  )}
                </div>
              ))}
            </div>
          </div>

          <div className="task-card stack">
            <div className="row spread">
              <strong>Reply times</strong>
              <select className="field" value={latDays} onChange={(e) => setLatDays(Number(e.target.value))} aria-label="Period">
                <option value={1}>Last 24 hours</option>
                <option value={7}>Last 7 days</option>
                <option value={30}>Last 30 days</option>
              </select>
            </div>
            {lat && lat.replies ? (
              <>
                <span className="muted">
                  {lat.replies} replies · median {secs(lat.p50_ms)} · 95% within {secs(lat.p95_ms)} · slowest {secs(lat.max_ms)} ·{" "}
                  <span className={lat.over_target ? "err" : ""}>
                    {lat.over_target} over {lat.target_s}s
                  </span>
                </span>
                {lat.over_target ? (
                  <span className="faint">
                    Time in replies over {lat.target_s}s: model {secs(lat.over_target_split.model_ms)} · connectors{" "}
                    {secs(lat.over_target_split.connector_ms)} · other tools {secs(lat.over_target_split.tool_ms)}
                  </span>
                ) : null}
                <ul className="admin-list">
                  {lat.slowest.slice(0, 10).map((r, n) => (
                    <li key={n}>
                      <span className="stack">
                        <span className={r.total_ms > lat.target_s * 1000 ? "err" : ""}>
                          {secs(r.total_ms)} · model {secs(r.model_ms)} ({r.model_calls} calls) · connectors {secs(r.connector_ms)}
                          {r.tool_ms ? ` · tools ${secs(r.tool_ms)}` : ""}
                          {r.computer_used ? " · computer" : ""}
                          {r.ended ? ` · ${r.ended}` : ""}
                        </span>
                        <span className="faint">
                          {[
                            r.prep_ms ? `checks before calls ${secs(r.prep_ms)}` : "",
                            r.stages?.finish_check ? `finish check ${secs(r.stages.finish_check)}` : "",
                            r.stages?.fact_check ? `fact check ${secs(r.stages.fact_check)}` : "",
                            r.other_ms ? `unaccounted ${secs(r.other_ms)}` : "",
                          ]
                            .filter(Boolean)
                            .join(" · ")}
                        </span>
                        {r.tools?.length ? (
                          <span className="faint">
                            {r.tools.slice(0, 6).map((t) => `${t.tool} ×${t.calls} ${secs(t.ms)}`).join(" · ")}
                          </span>
                        ) : null}
                        {r.slowest_calls.length ? (
                          <span className="faint">
                            {r.slowest_calls
                              .map((c) => `${c.connector}/${c.tool} ${c.cached ? "cached" : secs(c.ms)}${c.pages ? ` (${c.pages} pages, first ${secs(c.first_ms)})` : ""}`)
                              .join(" · ")}
                          </span>
                        ) : null}
                      </span>
                      <span className="faint">{r.user} · {new Date(r.at).toLocaleString()}</span>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <span className="faint">No timed replies yet. Every new chat reply is timed from now on.</span>
            )}
          </div>

          <div className="task-card stack">
            <div className="row spread">
              <strong>Test scenarios</strong>
              {run?.status === "running" ? (
                <button className="btn secondary small" onClick={stopScenarios}>Stop after this question</button>
              ) : (
                <button className="btn small" onClick={runScenarios} disabled={!picked.size && !custom.trim()}>
                  Run {picked.size + custom.split("\n").filter((x) => x.trim()).length} questions
                </button>
              )}
            </div>
            <span className="muted">
              Everyday questions run through the real agent as you, one after another, each in its own chat. Reads only: anything that
              would send or change something stays an approval card. Each reply is timed, and uses your connectors and tokens.
            </span>
            {[...new Set(scenarios.map((x) => x.group))].map((g) => (
              <div key={g} className="stack">
                <span className="faint">{g}</span>
                {scenarios
                  .filter((x) => x.group === g)
                  .map((x) => (
                    <label key={x.id} className="row">
                      <input type="checkbox" checked={picked.has(x.id)} onChange={() => toggle(x.id)} />
                      <span>
                        {x.text}
                        {x.after ? <span className="faint"> (follow-up, same chat)</span> : null}
                      </span>
                    </label>
                  ))}
              </div>
            ))}
            <textarea
              className="field"
              rows={3}
              placeholder="Your own questions, one per line (up to 10)"
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
            />
            {run ? (
              <>
                <span className="muted">
                  {run.status === "running" ? "Running" : `Run ${run.status}`} ·{" "}
                  {run.results.filter((x) => x.total_ms != null && !x.over_target).length} within {run.target_s}s ·{" "}
                  <span className={run.results.some((x) => x.over_target) ? "err" : ""}>
                    {run.results.filter((x) => x.over_target).length} over
                  </span>
                </span>
                <ul className="admin-list">
                  {run.results.map((x) => (
                    <li key={x.id}>
                      <span className="stack">
                        <span>{x.text}</span>
                        <span className={x.over_target || x.status === "error" || x.status === "timeout" ? "err" : "faint"}>
                          {x.status === "waiting" || x.status === "running" || x.status === "skipped"
                            ? x.status
                            : `${secs(x.total_ms)} · model ${secs(x.model_ms)} (${x.model_calls ?? 0} calls) · connectors ${secs(x.connector_ms)} · tools ${secs(x.tool_ms)} · other ${secs(x.other_ms)}${x.waiting_approval ? " · waiting for approval" : ""}${x.error ? ` · ${x.error}` : ""}`}
                        </span>
                        {x.tools?.length ? (
                          <span className="faint">{x.tools.slice(0, 5).map((t) => `${t.tool} ×${t.calls} ${secs(t.ms)}`).join(" · ")}</span>
                        ) : null}
                      </span>
                      {x.session_id ? (
                        <a className="btn secondary small" href={chatHref({ agent: x.agent_id, conversation: x.session_id })}>
                          Open chat
                        </a>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </div>

          <div className="task-card stack">
            <div className="row spread">
              <strong>Audit log</strong>
              <a className="btn secondary small" href="/api/admin/audit?days=90&fmt=csv">Export CSV (90 days)</a>
            </div>
            <ul className="admin-list">
              {audit.map((a, n) => (
                <li key={n}>
                  <span>{a.action.replace(/_/g, " ")} {a.target ? `· ${a.target}` : ""}</span>
                  <span className="faint">{a.user || a.actor} · {new Date(a.at).toLocaleString()}</span>
                </li>
              ))}
              {!audit.length ? <li className="faint">Nothing in the last 7 days.</li> : null}
            </ul>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
