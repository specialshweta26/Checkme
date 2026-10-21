"use client";

import { useState } from "react";

export type MailThreadRef = {
  connector_slug?: string;
  thread_id?: string;
  message_id?: string;
  subject?: string;
  item_id?: string;
};

type ThreadMsg = {
  message_id?: string;
  from?: string;
  to?: string;
  cc?: string;
  date?: string;
  subject?: string;
  body?: string;
};

export function mailRefsFromMeta(meta?: any): MailThreadRef[] {
  const rows = Array.isArray(meta?.mail_threads) ? meta.mail_threads : [];
  const refs = rows.filter((row: any) => row && (row.thread_id || row.message_id || row.item_id));
  if (refs.length) return refs;
  const args = meta?.source?.args || {};
  const slug = meta?.source?.connector_slug || "";
  if (args.thread_id || args.message_id) {
    return [{ connector_slug: slug, thread_id: args.thread_id, message_id: args.message_id }];
  }
  return [];
}

export function EmailThreadButton({ refs, label = "See full email" }: { refs: MailThreadRef[]; label?: string }) {
  const usable = refs.filter((row) => row.item_id || row.thread_id || row.message_id);
  if (!usable.length) return null;
  return (
    <div className="email-thread-actions">
      {usable.map((row, i) => (
        <EmailThreadPanel key={`${row.item_id || row.thread_id || row.message_id}-${i}`} refData={row} label={usable.length > 1 && row.subject ? `See full email: ${row.subject}` : label} />
      ))}
    </div>
  );
}

function EmailThreadPanel({ refData, label }: { refData: MailThreadRef; label: string }) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [messages, setMessages] = useState<ThreadMsg[] | null>(null);
  const [subject, setSubject] = useState(refData.subject || "");

  async function toggle() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (messages) return;
    setLoading(true);
    setError("");
    try {
      const url = refData.item_id
        ? `/api/inbox/${refData.item_id}/thread`
        : `/api/chat/thread?${new URLSearchParams({
            slug: refData.connector_slug || "",
            thread_id: refData.thread_id || "",
            message_id: refData.message_id || "",
          })}`;
      const res = await fetch(url, { credentials: "include" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = typeof data.detail === "string" ? data.detail : data.detail?.[0]?.msg;
        throw new Error(detail || "Could not load the full email");
      }
      setSubject(data.subject || refData.subject || "");
      setMessages(data.messages || []);
    } catch (err: any) {
      setError(err.message || "Could not load the full email");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="email-thread">
      <button className="rephrase-btn" type="button" onClick={toggle}>
        {open ? "Hide full email" : label}
      </button>
      {open ? (
        <div className="email-full">
          {subject ? <h3 className="email-full-subject">{subject}</h3> : null}
          {loading ? <p className="muted">Loading the full thread…</p> : null}
          {error ? <p className="err">{error}</p> : null}
          {(messages || []).map((msg, i) => (
            <article key={msg.message_id || i} className="email-msg">
              <p className="email-msg-meta">
                {msg.from || "Unknown sender"}
                {msg.date ? ` · ${msg.date}` : ""}
                {msg.to ? ` · To ${msg.to}` : ""}
              </p>
              <pre className="email-msg-body">{msg.body || "(no body)"}</pre>
            </article>
          ))}
          {!loading && !error && messages && messages.length === 0 ? <p className="muted">No messages in this thread.</p> : null}
        </div>
      ) : null}
    </div>
  );
}
