"use client";

import { useEffect, useState } from "react";
import { AppShell } from "../components/AppShell";
import { apiError, getMe, type Me } from "../lib";

// Plan and billing (apps/api/app/billing, docs/BILLING.md). Payment, cards and invoices live on Stripe's pages.

type Limits = { tokens: number | null; usd: number | null };
type Plan = {
  enabled: boolean;
  plan: "free" | "pro";
  status: string;
  renews_at: string | null;
  cancel_at_period_end: boolean;
  has_customer: boolean;
  today: { tokens: number; cost_usd: number };
  cap: Limits;
  plans: { free: Limits; pro: Limits };
  exempt: boolean;
};

const fmt = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n));
const limit = (l: Limits) => (l.tokens ? `${fmt(l.tokens)} tokens a day` : l.usd ? `$${l.usd} a day` : "No daily limit");

export default function BillingPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  useEffect(() => {
    setDone(new URLSearchParams(window.location.search).get("done") === "1");
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
      fetch("/api/billing", { credentials: "include" })
        .then((r) => (r.ok ? r.json() : null))
        .then(setPlan)
        .catch(() => undefined);
    });
  }, []);

  async function go(path: string) {
    setBusy(true);
    setError("");
    const res = await fetch(path, { method: "POST", credentials: "include" });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.url) {
      window.location.href = data.url;
      return;
    }
    setError(apiError(data, "That didn't work. Try again."));
    setBusy(false);
  }

  const used = plan?.cap.tokens ? Math.min(100, Math.round((plan.today.tokens / plan.cap.tokens) * 100)) : null;

  return (
    <AppShell me={me} current="billing">
      <div className="page">
        <div className="tasks-page">
          <div className="page-hero">
            <div>
              <h1>Plan and billing</h1>
              <p className="muted">Your plan, today&apos;s use against your daily limit, and payment.</p>
            </div>
          </div>
          {done ? <p className="notice">Thanks! Your plan updates as soon as Stripe confirms the payment, usually within seconds.</p> : null}
          {error ? <p className="err">{error}</p> : null}
          {plan ? (
            <div className="task-list">
              <div className="task-card">
                <div className="task-main stack">
                  <strong>
                    {plan.plan === "pro" ? "Pro" : "Free"} plan{plan.exempt ? " · no limit for your account" : ""}
                  </strong>
                  <span className="muted">
                    Today: {fmt(plan.today.tokens)} tokens{plan.cap.tokens ? ` of ${fmt(plan.cap.tokens)}` : ""}
                    {plan.cap.usd ? ` · $${plan.today.cost_usd.toFixed(2)} of $${plan.cap.usd}` : ""}. The limit resets at midnight UTC.
                  </span>
                  {used !== null ? (
                    <div className="meter" aria-label={`${used}% of today's limit used`}>
                      <span style={{ width: `${used}%` }} />
                    </div>
                  ) : null}
                  {plan.plan === "pro" && plan.renews_at ? (
                    <span className="faint">
                      {plan.cancel_at_period_end ? "Ends" : "Renews"} {new Date(plan.renews_at).toLocaleDateString()}
                      {plan.status === "past_due" ? " · payment failed, update your card" : ""}
                    </span>
                  ) : null}
                </div>
                <div className="task-actions stack">
                  {plan.enabled && plan.plan === "free" ? (
                    <button className="btn" disabled={busy} onClick={() => go("/api/billing/checkout")}>
                      {busy ? "Opening…" : "Upgrade to Pro"}
                    </button>
                  ) : null}
                  {plan.enabled && plan.has_customer ? (
                    <button className="btn secondary" disabled={busy} onClick={() => go("/api/billing/portal")}>
                      Manage billing
                    </button>
                  ) : null}
                </div>
              </div>
              {plan.enabled ? (
                <div className="task-card">
                  <div className="task-main stack">
                    <strong>Plans</strong>
                    <span className="muted">Free: {limit(plan.plans.free)}</span>
                    <span className="muted">Pro: {limit(plan.plans.pro)}</span>
                    <span className="faint">Payments, cards and invoices are handled by Stripe. Lumio never sees your card.</span>
                  </div>
                </div>
              ) : (
                <p className="muted">Paid plans aren&apos;t offered on this server.</p>
              )}
            </div>
          ) : null}
        </div>
      </div>
    </AppShell>
  );
}
