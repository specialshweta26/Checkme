"use client";

import { useEffect, useState } from "react";
import { getAgentBudget, setAgentLimits, type AgentBudget, type WorkspaceAgent } from "../lib";

// Budgets and limits (docs/BUDGETS.md), in the agent's About panel: what it spent today and this month, and the caps.
// Past a budget, chat replies say so without calling a model and background tasks pause and ask; past the time limit,
// a task run pauses and asks to keep going.

const money = (n: number) => `$${n.toFixed(2)}`;
const count = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`);
const OVER: Record<string, string> = { daily: "daily budget", monthly: "monthly budget", tokens: "daily token limit" };

function Meter({ label, spent, cap, fmt = money }: { label: string; spent: number; cap?: number; fmt?: (n: number) => string }) {
  const pct = cap ? Math.min(100, (spent / cap) * 100) : 0;
  return (
    <div className="limits-meter">
      <div>
        <span>{label}</span>
        <strong>
          {fmt(spent)}
          {cap ? <span className="faint"> of {fmt(cap)}</span> : null}
        </strong>
      </div>
      {cap ? (
        <div className={`limits-bar${pct >= 100 ? " over" : pct >= 80 ? " near" : ""}`} role="progressbar" aria-label={`${label} spend`} aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
          <span style={{ width: `${pct}%` }} />
        </div>
      ) : null}
    </div>
  );
}

export function AgentLimits({ agent }: { agent: WorkspaceAgent }) {
  const [data, setData] = useState<AgentBudget | null>(null);
  const [form, setForm] = useState({ daily: "", monthly: "", tokens: "", minutes: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function fill(d: AgentBudget) {
    setData(d);
    setForm({
      daily: d.limits.daily_usd?.toString() || "",
      monthly: d.limits.monthly_usd?.toString() || "",
      tokens: d.limits.daily_tokens?.toString() || "",
      minutes: d.limits.task_minutes?.toString() || "",
    });
  }

  useEffect(() => {
    setData(null);
    setError("");
    setNotice("");
    getAgentBudget(agent.id).then(fill).catch((e) => setError(e.message));
  }, [agent.id]);

  const num = (v: string) => (v.trim() === "" ? null : Number(v));
  const l = data?.limits || {};
  const dirty =
    !!data &&
    (form.daily !== (l.daily_usd?.toString() || "") ||
      form.monthly !== (l.monthly_usd?.toString() || "") ||
      form.tokens !== (l.daily_tokens?.toString() || "") ||
      form.minutes !== (l.task_minutes?.toString() || ""));

  async function save() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      fill(await setAgentLimits(agent.id, { daily_usd: num(form.daily), monthly_usd: num(form.monthly), daily_tokens: num(form.tokens), task_minutes: num(form.minutes) }));
      setNotice("Saved.");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="limits">
      <p className="about-label">Limits</p>
      {data ? (
        <>
          {data.over ? (
            <p className="limits-over" role="status">
              Over its {OVER[data.over]}. Chats get a short note instead of a reply, and background tasks wait for your OK. Raise the limit to carry on.
            </p>
          ) : null}
          <div className="limits-meters">
            {data.priced ? <Meter label="Today" spent={data.today_usd} cap={l.daily_usd} /> : null}
            {data.priced ? <Meter label="This month" spent={data.month_usd} cap={l.monthly_usd} /> : null}
            <Meter label="Tokens today" spent={data.today_tokens} cap={l.daily_tokens} fmt={count} />
          </div>
          {!data.priced ? (
            <p className="faint about-note">Model prices aren’t set on this server, so dollar budgets can’t be counted yet. The token limit works without them.</p>
          ) : data.unpriced ? (
            <p className="faint about-note">Some of its use is on models without a price, which dollar budgets don’t count.</p>
          ) : null}
        </>
      ) : !error ? (
        <p className="faint about-note">Loading spend…</p>
      ) : null}
      <form
        className="limits-form"
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <label>
          <span>Daily budget</span>
          <span className="limits-input">
            <span aria-hidden>$</span>
            <input inputMode="decimal" type="number" min="0" step="0.01" placeholder="No limit" value={form.daily} onChange={(e) => setForm({ ...form, daily: e.target.value })} />
          </span>
        </label>
        <label>
          <span>Monthly budget</span>
          <span className="limits-input">
            <span aria-hidden>$</span>
            <input inputMode="decimal" type="number" min="0" step="0.01" placeholder="No limit" value={form.monthly} onChange={(e) => setForm({ ...form, monthly: e.target.value })} />
          </span>
        </label>
        <label>
          <span>Tokens per day</span>
          <span className="limits-input">
            <input inputMode="numeric" type="number" min="0" step="1000" placeholder="No limit" value={form.tokens} onChange={(e) => setForm({ ...form, tokens: e.target.value })} />
          </span>
        </label>
        <label>
          <span>Time per task run</span>
          <span className="limits-input">
            <input inputMode="numeric" type="number" min="0" step="1" placeholder="No limit" value={form.minutes} onChange={(e) => setForm({ ...form, minutes: e.target.value })} />
            <span aria-hidden>min</span>
          </span>
        </label>
        <button className="btn small" type="submit" disabled={busy || !dirty}>
          {busy ? "Saving…" : "Save limits"}
        </button>
      </form>
      <p className="faint about-note">Budgets count model spend in chats, groups and background tasks (days in UTC). Leave a field empty for no limit.</p>
      {error ? <p className="err">{error}</p> : null}
      {notice && !dirty ? <p className="faint about-note">{notice}</p> : null}
    </div>
  );
}
