"use client";

import { useEffect, useState } from "react";
import { AppShell } from "../components/AppShell";
import { getMe, type Me } from "../lib";

// Usage and cost for the signed-in user (GET /api/me/usage, apps/api/app/agent/meter.py): model tokens, searches and
// computer starts per feature for the last 7 days, and today's background budget.

type Totals = { calls: number; input: number; cached: number; output: number; cost_usd: number; searches: number; computer_starts: number };
type Row = { day: string; feature: string; model: string; calls: number; input: number; cached: number; output: number; units: number; cost_usd: number | null };
type Usage = {
  since: string;
  rows: Row[];
  by_feature: Record<string, Totals>;
  priced: boolean;
  background_today: { cost_usd: number; tokens: number; limit_usd: number | null; limit_tokens: number | null };
  over_budget: boolean;
  triage: { rule: number; small: number; full: number };
  drafts: { week: string; unchanged: number; light: number; rewritten: number }[];
};

const FEATURES: Record<string, string> = {
  chat: "Chat (your conversations, and remembering them)",
  inbox: "Inbox (triage, summaries, drafts)",
  memory: "Memory (learning from mail and calendar, nightly tidy-up)",
  jobs: "Scheduled tasks and automations",
  other: "Other",
};

const n = (v: number) => v.toLocaleString();
const usd = (v: number) => (v < 0.01 && v > 0 ? "< $0.01" : `$${v.toFixed(2)}`);

export default function UsagePage() {
  const [me, setMe] = useState<Me | null>(null);
  const [data, setData] = useState<Usage | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
      fetch("/api/me/usage?days=7", { credentials: "include" })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error("Couldn't load usage."))))
        .then(setData)
        .catch((e) => setError(e.message));
    });
  }, []);

  const bg = data?.background_today;
  const limit = bg?.limit_usd ? bg.cost_usd / bg.limit_usd : bg?.limit_tokens ? bg.tokens / bg.limit_tokens : null;
  const features = data ? Object.entries(data.by_feature).sort((a, b) => b[1].input + b[1].output - (a[1].input + a[1].output)) : [];
  const total = features.reduce((s, [, t]) => s + t.cost_usd, 0);

  return (
    <AppShell me={me} current="usage">
      <div className="page">
        <div className="tasks-page">
          <div className="page-hero">
            <div>
              <h1>Usage and cost</h1>
              <p className="muted">What your assistant used for you in the last 7 days: AI model tokens, web searches and starts of your cloud computer.</p>
            </div>
          </div>
          {error ? <p className="err">{error}</p> : null}
          {!data && !error ? <p className="muted">Loading…</p> : null}
          {data ? (
            <div className="task-list">
              <div className="task-card">
                <div className="task-main">
                  <strong>Background work today</strong>
                  <span className="muted">
                    Inbox triage and memory learning run without you asking.{" "}
                    {limit === null
                      ? "There is no daily limit."
                      : data.over_budget
                        ? "Today's limit is reached: until tomorrow, only automated mail is sorted and learning that needs AI pauses. Chat is not affected."
                        : "Within today's limit."}
                  </span>
                  {limit !== null ? (
                    <div className="usage-bar" aria-label="Share of today's background limit used">
                      <span style={{ width: `${Math.min(100, Math.round(limit * 100))}%` }} />
                    </div>
                  ) : null}
                  <span className="faint">
                    {bg?.limit_usd
                      ? `${usd(bg.cost_usd)} of ${usd(bg.limit_usd)}`
                      : bg?.limit_tokens
                        ? `${n(bg.tokens)} of ${n(bg.limit_tokens)} tokens`
                        : `${n(bg?.tokens || 0)} tokens${data.priced ? `, ${usd(bg?.cost_usd || 0)}` : ""}`}
                  </span>
                </div>
              </div>

              <div className="task-card">
                <div className="task-main">
                  <strong>How your inbox was sorted (last 7 days)</strong>
                  {data.triage && data.triage.rule + data.triage.small + data.triage.full > 0 ? (
                    <>
                      <span className="muted">
                        {n(data.triage.rule)} by simple rules (no AI) · {n(data.triage.small)} by the small, cheap model ·{" "}
                        {n(data.triage.full)} by the full model (these got a drafted reply)
                      </span>
                      <span className="faint">
                        {Math.round(((data.triage.rule + data.triage.small) / (data.triage.rule + data.triage.small + data.triage.full)) * 100)}% of
                        emails never needed the full model.
                      </span>
                    </>
                  ) : (
                    <span className="muted">No new mail sorted yet this week.</span>
                  )}
                </div>
              </div>

              <div className="task-card">
                <div className="task-main">
                  <strong>How much you change the drafted replies</strong>
                  <span className="muted">
                    Each reply you send from the inbox, compared with the assistant's draft. More "as drafted" over the weeks means it's learning how you
                    write.
                  </span>
                  {data.drafts && data.drafts.length ? (
                    <div className="table-scroll"><table className="usage-table">
                      <thead>
                        <tr>
                          <th>Week of</th>
                          <th>Sent as drafted</th>
                          <th>Lightly edited</th>
                          <th>Rewritten</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.drafts.map((w) => (
                          <tr key={w.week}>
                            <td>{new Date(w.week).toLocaleDateString(undefined, { day: "numeric", month: "short" })}</td>
                            <td>{w.unchanged}</td>
                            <td>{w.light}</td>
                            <td>{w.rewritten}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table></div>
                  ) : (
                    <span className="faint">No replies sent from the inbox yet. This fills in as you send.</span>
                  )}
                </div>
              </div>

              <div className="task-card">
                <div className="task-main">
                  <strong>Last 7 days by feature{data.priced ? ` · ${usd(total)} in total` : ""}</strong>
                  {features.length === 0 ? (
                    <span className="muted">Nothing recorded yet. Usage appears here as you use the assistant.</span>
                  ) : (
                    <div className="table-scroll"><table className="usage-table">
                      <thead>
                        <tr>
                          <th>Feature</th>
                          <th>AI calls</th>
                          <th>Tokens in (cached)</th>
                          <th>Tokens out</th>
                          <th>Searches</th>
                          <th>Computer starts</th>
                          {data.priced ? <th>Cost</th> : null}
                        </tr>
                      </thead>
                      <tbody>
                        {features.map(([feature, t]) => (
                          <tr key={feature}>
                            <td>{FEATURES[feature] || feature}</td>
                            <td>{n(t.calls)}</td>
                            <td>
                              {n(t.input)} ({t.input ? Math.round((t.cached / t.input) * 100) : 0}%)
                            </td>
                            <td>{n(t.output)}</td>
                            <td>{n(t.searches)}</td>
                            <td>{n(t.computer_starts)}</td>
                            {data.priced ? <td>{usd(t.cost_usd)}</td> : null}
                          </tr>
                        ))}
                      </tbody>
                    </table></div>
                  )}
                  {!data.priced ? <span className="faint">Costs appear when model prices are set on the server (MODEL_PRICES).</span> : null}
                </div>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </AppShell>
  );
}
