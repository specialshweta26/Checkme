"use client";

import { useEffect, useMemo, useState } from "react";
import { AppShell } from "../components/AppShell";
import { getMe, type Me } from "../lib";

type Classification = "reply" | "respond" | "discard" | "";
type Filter = "all" | Classification;

type InboxMail = {
  id: string;
  message_id: string;
  thread_id: string;
  subject: string;
  from: string;
  date: string;
  snippet: string;
  classification: Classification;
  summary: string;
  key_points: string[];
  draft: string;
  status: string;
  unread: boolean;
  seen: boolean;
  connector: string;
  sort_at?: string;
};

const TONES = ["Professional", "Concise", "Friendly", "Warmer"] as const;

export default function InboxPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [items, setItems] = useState<InboxMail[]>([]);
  const [connected, setConnected] = useState(true);
  const [filter, setFilter] = useState<Filter>("all");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [openRephrase, setOpenRephrase] = useState("");
  const [instruction, setInstruction] = useState("");
  const [focusId, setFocusId] = useState("");

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/");
        return;
      }
      setMe(u);
    });
    setFocusId(new URLSearchParams(window.location.search).get("mail") || "");
  }, []);

  async function refresh() {
    const res = await fetch("/api/inbox", { credentials: "include" });
    if (res.status === 401) {
      window.location.replace("/");
      return;
    }
    if (!res.ok) {
      const text = await res.text();
      setError(text || "Could not load inbox");
      setLoading(false);
      return;
    }
    const data = await res.json();
    setItems(data.items || []);
    setConnected(data.connected !== false);
    setError("");
    setLoading(false);
    window.dispatchEvent(new Event("inbox-seen"));
  }

  useEffect(() => {
    if (!me) return;
    refresh();
    const id = window.setInterval(refresh, 45000);
    return () => window.clearInterval(id);
  }, [me]);

  useEffect(() => {
    if (!items.some((m) => !m.classification && m.status === "pending")) return;
    const id = window.setTimeout(() => refresh(), 2500);
    return () => window.clearTimeout(id);
  }, [items]);

  const visible = useMemo(() => {
    const rows = filter === "all" ? items : items.filter((m) => m.classification === filter);
    return [...rows].sort((a, b) => {
      const left = Date.parse(a.sort_at || "") || 0;
      const right = Date.parse(b.sort_at || "") || 0;
      return right - left;
    });
  }, [items, filter]);

  useEffect(() => {
    if (focusId) setFilter("all");
  }, [focusId]);

  useEffect(() => {
    if (!focusId || loading) return;
    const node = document.getElementById(`mail-${focusId}`);
    if (!node) return;
    node.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [focusId, loading, visible]);

  function update(id: string, patch: Partial<InboxMail>) {
    setItems((rows) => rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  async function act(id: string, path: string, body?: Record<string, string>) {
    setBusy(`${id}:${path}`);
    setError("");
    const item = items.find((m) => m.id === id);
    const res = await fetch(`/api/inbox/${id}${path}`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft: item?.draft || "", ...body }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setError(typeof data.detail === "string" ? data.detail : "That action failed");
      setBusy("");
      return;
    }
    if (path === "/send" || path === "/delete") {
      setItems((rows) => rows.filter((r) => r.id !== id));
    } else if (data.draft || data.classification) {
      update(id, data);
    } else if (data.item) {
      update(id, data.item);
    }
    setBusy("");
    setOpenRephrase("");
  }

  async function saveDraft(id: string, draft: string) {
    update(id, { draft });
    await fetch(`/api/inbox/${id}`, {
      method: "PATCH",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft }),
    });
  }

  if (!me) return <div className="center">Loading your workspace…</div>;

  return (
    <AppShell me={me} current="inbox">
      <div className="page inbox-page">
        <div className="page-hero">
          <div>
            <h1>Inbox</h1>
            <p className="muted">Triage arriving mail, review the thread, and send a drafted reply.</p>
          </div>
          <button className="btn secondary" type="button" onClick={() => { setLoading(true); refresh(); }} disabled={loading}>
            Refresh
          </button>
        </div>

        <div className="inbox-toolbar">
          {(["all", "reply", "respond", "discard"] as Filter[]).map((id) => (
            <button key={id} className={`pill${filter === id ? " on" : ""}`} onClick={() => setFilter(id)}>
              {id === "all" ? "All" : id[0].toUpperCase() + id.slice(1)}
              {id !== "all" ? <span className="faint">{items.filter((m) => m.classification === id).length}</span> : null}
            </button>
          ))}
        </div>

        {error ? <p className="err">{error}</p> : null}

        {!connected ? (
          <div className="task-empty">
            <p className="muted">Connect Gmail or Outlook to triage incoming mail.</p>
            <a className="btn" href="/connectors">
              Open connectors
            </a>
          </div>
        ) : loading ? (
          <p className="muted">Reading your inbox and drafting replies…</p>
        ) : visible.length === 0 ? (
          <div className="task-empty">
            <p className="muted">No messages in this view. New mail will appear here with a drafted reply.</p>
          </div>
        ) : (
          <div className="inbox-stack">
            {visible.map((mail) => {
              const working = busy.startsWith(mail.id);
              return (
                <article id={`mail-${mail.id}`} key={mail.id} className={`inbox-card${focusId === mail.id ? " focus" : ""}`}>
                  <header className="inbox-card-top">
                    <span className={`triage-tag ${mail.classification || "pending"}`}>
                      {mail.classification || "reviewing"}
                    </span>
                    <span className={`status-dot ${mail.status === "pending" ? "active" : "ok"}`}>
                      {mail.status === "pending" ? "Pending" : "Completed"}
                    </span>
                  </header>
                  <h2>{mail.subject || "(no subject)"}</h2>
                  <p className="inbox-meta">
                    {mail.from || "Unknown sender"}
                    {mail.date ? ` · ${mail.date}` : ""}
                    {mail.connector ? ` · ${mail.connector}` : ""}
                  </p>
                  {mail.summary ? <p className="inbox-summary">{mail.summary}</p> : <p className="muted">{mail.snippet || "Reading this thread…"}</p>}
                  {mail.key_points?.length ? (
                    <ul className="inbox-points">
                      {mail.key_points.map((point) => (
                        <li key={point}>{point}</li>
                      ))}
                    </ul>
                  ) : null}
                  <label className="inbox-draft-label">Drafted reply</label>
                  <textarea
                    className="inbox-draft"
                    value={mail.draft}
                    placeholder="A drafted reply will appear here after the thread is reviewed."
                    onChange={(e) => update(mail.id, { draft: e.target.value })}
                    onBlur={(e) => saveDraft(mail.id, e.target.value)}
                  />
                  {openRephrase === mail.id ? (
                    <div className="inbox-rephrase">
                      {TONES.map((tone) => (
                        <button
                          key={tone}
                          className="chip"
                          type="button"
                          disabled={working}
                          onClick={() => act(mail.id, "/rephrase", { tone: tone.toLowerCase(), instruction })}
                        >
                          {tone}
                        </button>
                      ))}
                      <input
                        value={instruction}
                        onChange={(e) => setInstruction(e.target.value)}
                        placeholder="Custom instruction"
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && instruction.trim()) {
                            act(mail.id, "/rephrase", { tone: "custom", instruction });
                          }
                        }}
                      />
                    </div>
                  ) : null}
                  <footer className="inbox-actions">
                    <button className="rephrase-btn" type="button" disabled={working} onClick={() => setOpenRephrase(openRephrase === mail.id ? "" : mail.id)}>
                      ✦ Rephrase
                    </button>
                    <button
                      className="send-btn"
                      type="button"
                      disabled={working || !mail.draft.trim()}
                      onClick={() => act(mail.id, "/send")}
                    >
                      Send
                    </button>
                    <button className="icon-btn" type="button" disabled={working} onClick={() => act(mail.id, "/delete")}>
                      Delete
                    </button>
                  </footer>
                </article>
              );
            })}
          </div>
        )}
      </div>
    </AppShell>
  );
}
