"use client";

import { useCallback, useEffect, useState } from "react";

// Cards in Chief's window for mail that really needs the user (apps/api/app/inbox/alerts.py): a reply from a key
// contact, something urgent, or something about a promise. Built from the inbox's own summary and draft, and kept out
// of the chat transcript, so they never cost tokens or crowd the conversation.

type Alert = {
  id: string;
  subject: string;
  from: string;
  summary: string;
  key_points: string[];
  draft: string;
  alert_reason: string;
  alert_label: string;
  sender_note?: string;
};

const SHOW = 3;
const REFRESH_MS = 60_000;

function senderName(from: string) {
  const name = (from || "").split("<")[0].replace(/"/g, "").trim();
  return name || (from || "").replace(/[<>]/g, "") || "Someone";
}

export function ChiefAlerts() {
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [editing, setEditing] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [done, setDone] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(() => {
    fetch("/api/inbox/alerts", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : { alerts: [] }))
      .then((d) => setAlerts(d.alerts || []))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    load();
    const id = window.setInterval(load, REFRESH_MS);
    const onFocus = () => load();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(id);
      window.removeEventListener("focus", onFocus);
    };
  }, [load]);

  async function post(id: string, path: string, body: Record<string, unknown>) {
    setBusy(id);
    setError("");
    const res = await fetch(`/api/inbox/${id}${path}`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    setBusy("");
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      setError(typeof data.detail === "string" ? data.detail : "That didn't work. Try again, or open it in the inbox.");
      return false;
    }
    return true;
  }

  async function send(a: Alert) {
    const draft = editing[a.id] ?? a.draft;
    if (await post(a.id, "/send", { draft })) {
      setDone((cur) => ({ ...cur, [a.id]: `Sent to ${senderName(a.from)}.` }));
      window.setTimeout(() => setAlerts((rows) => rows.filter((r) => r.id !== a.id)), 2500);
    }
  }

  async function notNow(a: Alert) {
    if (await post(a.id, "/alert", { state: "dismissed" })) setAlerts((rows) => rows.filter((r) => r.id !== a.id));
  }

  if (!alerts.length) return null;
  const visible = expanded ? alerts : alerts.slice(0, SHOW);

  return (
    <section className="chief-alerts" aria-label="Emails that need you">
      <div className="chief-alerts-head">
        <strong>{alerts.length === 1 ? "1 email needs you" : `${alerts.length} emails need you`}</strong>
        <span className="faint">From your inbox. Nothing is sent until you press Send.</span>
      </div>
      {error ? <p className="err">{error}</p> : null}
      {visible.map((a) => {
        const isEditing = a.id in editing;
        return (
          <article key={a.id} className="chief-alert">
            <header>
              <strong>{senderName(a.from)} needs a reply</strong>
              <span className={`chief-alert-tag ${a.alert_reason}`}>{a.alert_label}</span>
            </header>
            <p className="chief-alert-subject">{a.subject || "(no subject)"}</p>
            {a.summary ? <p className="muted">{a.summary}</p> : null}
            {done[a.id] ? (
              <p className="att-summary">{done[a.id]}</p>
            ) : (
              <>
                {isEditing ? (
                  <textarea className="field" rows={4} value={editing[a.id]} onChange={(e) => setEditing((cur) => ({ ...cur, [a.id]: e.target.value }))} />
                ) : a.draft ? (
                  <blockquote className="chief-alert-draft">{a.draft}</blockquote>
                ) : null}
                <div className="chief-alert-actions">
                  <button className="btn" disabled={busy === a.id || !(editing[a.id] ?? a.draft).trim()} onClick={() => send(a)}>
                    {busy === a.id ? "Sending…" : "Send"}
                  </button>
                  {isEditing ? (
                    <button
                      className="btn secondary"
                      onClick={() =>
                        setEditing((cur) => {
                          const next = { ...cur };
                          delete next[a.id];
                          return next;
                        })
                      }
                    >
                      Cancel edit
                    </button>
                  ) : (
                    <button className="btn secondary" onClick={() => setEditing((cur) => ({ ...cur, [a.id]: a.draft }))}>
                      Edit
                    </button>
                  )}
                  <a className="btn secondary" href={`/inbox?mail=${encodeURIComponent(a.id)}`}>
                    Open in inbox
                  </a>
                  <button className="btn secondary" disabled={busy === a.id} onClick={() => notNow(a)} title="Hide this card. The email stays in your inbox.">
                    Not now
                  </button>
                </div>
              </>
            )}
          </article>
        );
      })}
      {alerts.length > SHOW ? (
        <button className="chip" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "Show fewer" : `Show ${alerts.length - SHOW} more`}
        </button>
      ) : null}
    </section>
  );
}
