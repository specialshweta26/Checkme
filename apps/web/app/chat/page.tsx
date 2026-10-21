"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { AttachmentWidget } from "../AttachmentWidget";
import { AppShell } from "../components/AppShell";
import { EmailThreadButton, mailRefsFromMeta } from "../components/EmailThread";
import { Markdown } from "../components/Markdown";
import { Thinking } from "../components/Thinking";
import { deleteSession, getMe, listSessions, loadSession, uploadFiles, type ChatSessionRow, type Me, type UploadedFile } from "../lib";

const STORE = "assistant.session";

type Msg = { role: "user" | "assistant"; content: string; meta?: any };
type Confirm = { token: string; title: string; preview: any };

export default function ChatPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<ChatSessionRow[]>([]);
  const [pending, setPending] = useState<Confirm[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploads, setUploads] = useState<UploadedFile[]>([]);
  const [uploadError, setUploadError] = useState("");

  async function refreshSessions() {
    setSessions(await listSessions());
  }

  async function openSession(id: string) {
    const data = await loadSession(id);
    if (!data) return;
    setSessionId(id);
    window.localStorage.setItem(STORE, id);
    setMessages(
      (data.messages || [])
        .filter((m: Msg) => m.role === "user" || m.role === "assistant")
        .map((m: Msg) => ({ role: m.role, content: m.content || "", meta: m.meta })),
    );
    setPending([]);
  }

  function newChat() {
    setSessionId(null);
    setMessages([]);
    setPending([]);
    setNotice("");
    setUploads([]);
    window.localStorage.removeItem(STORE);
    box.current?.focus();
  }

  useEffect(() => {
    getMe().then(async (u) => {
      if (!u) {
        window.location.replace("/");
        return;
      }
      if (!u.timezone || u.timezone === "UTC") {
        await fetch("/api/me", {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ timezone: "Asia/Kolkata" }),
        }).catch(() => undefined);
        u = { ...u, timezone: "Asia/Kolkata" };
      }
      setMe(u);
      const rows = await listSessions();
      setSessions(rows);
      const wanted = new URLSearchParams(window.location.search).get("c") || window.localStorage.getItem(STORE);
      if (wanted && rows.some((r) => r.id === wanted)) await openSession(wanted);
    });
    const q = new URLSearchParams(window.location.search);
    if (q.get("mcp") === "connected") setNotice("MCP connected. You can use it in this chat.");
    if (q.get("error")) setNotice(q.get("error")?.replaceAll("_", " ") || "");
  }, []);

  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pending, busy]);

  async function send(message: string, confirmations: any[] = []) {
    if ((!message.trim() && !uploads.length) || busy) return;
    const pendingFiles = uploads;
    setBusy(true);
    setMessages((m) => [...m, { role: "user", content: message || "Attached files", meta: pendingFiles.length ? { uploads: pendingFiles } : undefined }]);
    setText("");
    setUploads([]);
    setUploadError("");
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, session_id: sessionId, confirmations, attachments: pendingFiles }),
      });
      const raw = await res.text();
      let data: any = {};
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch {
        throw new Error(raw.slice(0, 280) || "Chat failed");
      }
      if (!res.ok) {
        const detail = typeof data.detail === "string" ? data.detail : data.detail?.[0]?.msg;
        throw new Error(detail || raw.slice(0, 280) || "Chat failed");
      }
      setSessionId(data.session_id);
      if (data.session_id) window.localStorage.setItem(STORE, data.session_id);
      setMessages((m) => [...m, { role: "assistant", content: data.text || "", meta: data }]);
      setPending(data.pending_confirms || []);
      await refreshSessions();
    } catch (err: any) {
      setMessages((m) => [...m, { role: "assistant", content: err.message || "Something went wrong" }]);
    } finally {
      setBusy(false);
    }
  }

  async function removeChat(id: string) {
    await deleteSession(id);
    if (sessionId === id) newChat();
    await refreshSessions();
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    send(text);
  }

  if (!me) return <div className="center">Loading your workspace…</div>;

  return (
    <AppShell
      me={me}
      current="chat"
      conversations={sessions}
      activeId={sessionId}
      onNewChat={newChat}
      onOpenChat={openSession}
      onDeleteChat={removeChat}
    >
      <div className="chat">
        <div className="transcript">
          <div className="thread">
            {notice ? <p className="notice">{notice}</p> : null}
            {messages.length === 0 && !busy ? (
              <div className="welcome">
                <h1>{greeting(me)}</h1>
                <p>Here&apos;s what&apos;s happening today — brief the inbox, ask a connected system, or schedule a send.</p>
                <div className="hero-cards">
                  <button className="hero-card" onClick={() => send("What matters today? Give me my chief of staff briefing.")}>
                    <span className="mark" />
                    <strong>Summarize emails</strong>
                    <span>Inbox, calendar, and the decisions that cannot wait.</span>
                  </button>
                  <button className="hero-card" onClick={() => send("What am I waiting for, and what did I promise?")}>
                    <span className="mark" />
                    <strong>Check connected systems</strong>
                    <span>Promises, waits, or any tool already linked to this workspace.</span>
                  </button>
                  <a className="hero-card" href="/tasks">
                    <span className="mark" />
                    <strong>Schedule a send</strong>
                    <span>Work that should run at a time you choose, then go out.</span>
                  </a>
                </div>
              </div>
            ) : null}
            {messages.map((m, i) =>
              m.role === "user" ? (
                <div key={i} className="turn user">
                  <div className="bubble">{m.content}</div>
                  {m.meta?.uploads?.length ? <AttachmentWidget items={m.meta.uploads} /> : null}
                </div>
              ) : (
                <div key={i} className="turn assistant">
                  <Markdown text={m.content} />
                  {sourceLabel(m.meta) ? <div className="source-chip">{sourceLabel(m.meta)}</div> : null}
                  <EmailThreadButton refs={mailRefsFromMeta(m.meta)} />
                  {m.meta?.attachments?.length ? <AttachmentWidget items={m.meta.attachments} /> : null}
                  {m.meta?.clarify?.length ? (
                    <div className="chips" style={{ marginTop: 8 }}>
                      {m.meta.clarify.map((c: any) => (
                        <button key={c.id} className="chip" onClick={() => send(`Use @${c.slug || c.name} for that.`)}>
                          {c.name}
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
              ),
            )}
            {busy ? <Thinking /> : null}
            <div ref={end} />
          </div>
        </div>
        {pending.length ? (
          <div className="approval">
            {pending.map((p) => (
              <div key={p.token}>
                <h3>{p.title || "Approve this action"}</h3>
                <p className="muted" style={{ margin: "0 0 8px" }}>
                  Review the recipient and preview, then confirm. Nothing sends until you do.
                </p>
                <pre className="approval-preview">{formatPreview(p.preview)}</pre>
                <div className="approval-actions">
                  <button
                    className="btn"
                    disabled={busy}
                    onClick={() => {
                      setPending([]);
                      send("Confirmed. Continue.", pending);
                    }}
                  >
                    Confirm
                  </button>
                  <button className="btn secondary" onClick={() => setPending([])}>
                    Cancel
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : null}
        <div className="composer-wrap">
          {uploadError ? <p className="err">{uploadError}</p> : null}
          {uploads.length ? (
            <div className="attach-chips composer-chips">
              {uploads.map((file) => (
                <span key={file.key} className="attach-chip">
                  {file.filename}
                  <button type="button" aria-label="Remove file" onClick={() => setUploads((rows) => rows.filter((f) => f.key !== file.key))}>
                    ×
                  </button>
                </span>
              ))}
            </div>
          ) : null}
          <form className="composer" onSubmit={onSubmit}>
            <input
              ref={fileRef}
              type="file"
              multiple
              hidden
              onChange={async (e) => {
                const picked = Array.from(e.target.files || []);
                e.target.value = "";
                if (!picked.length) return;
                try {
                  setUploadError("");
                  const uploaded = await uploadFiles(picked);
                  setUploads((rows) => [...rows, ...uploaded]);
                } catch (err: any) {
                  setUploadError(err.message || "Upload failed");
                }
              }}
            />
            <button className="attach-icon" type="button" aria-label="Attach files" disabled={busy} onClick={() => fileRef.current?.click()}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
                <path d="M16 7v8.5a4 4 0 0 1-8 0V7.8A2.8 2.8 0 0 1 13.6 7v7.2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
              </svg>
            </button>
            <textarea
              ref={box}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Ask about sales, mail, or schedule work…"
              rows={1}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send(text);
                }
              }}
            />
            <button className="send" type="submit" disabled={busy || (!text.trim() && !uploads.length)} aria-label="Send">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                <path d="M12 19V5M12 5 6 11M12 5l6 6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </form>
        </div>
      </div>
    </AppShell>
  );
}

function greeting(me: Me) {
  const raw = (me.name || me.email || "there").trim().split(/[\s@]+/)[0];
  const first = raw ? raw[0].toUpperCase() + raw.slice(1) : "there";
  const hour = Number(
    new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "numeric", hour12: false }).format(new Date()),
  );
  const when = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  return `${when}, ${first}`;
}

function prettyTool(raw: string) {
  const last = raw.split(/--|__/).pop() || raw;
  return last.replace(/^get_/, "").replace(/_/g, " ");
}

function sourceLabel(meta?: any): string | null {
  if (!meta) return null;
  const names: string[] = [];
  const tools = meta.used_tools;
  if (Array.isArray(tools)) {
    for (const t of tools) {
      const raw = typeof t === "string" ? t : t?.name || t?.tool || t?.slug || "";
      if (raw) names.push(prettyTool(String(raw)));
    }
  }
  if (!names.length && typeof meta.source === "string" && meta.source.trim()) names.push(prettyTool(meta.source));
  if (!names.length && meta.source?.name) names.push(prettyTool(String(meta.source.name)));
  const unique = [...new Set(names.filter(Boolean))];
  return unique.length ? unique.slice(0, 2).join(" · ") : null;
}

function formatPreview(preview: any): string {
  if (!preview) return "No preview.";
  if (typeof preview === "string") return preview;
  const order = ["folder", "count", "to", "cc", "bcc", "reply_all", "recipient", "subject", "when", "schedule", "body", "preview", "text", "sample"];
  const lines = order
    .filter((k) => preview[k] != null && preview[k] !== "")
    .map((k) => {
      const label = k[0].toUpperCase() + k.slice(1);
      const value = typeof preview[k] === "string" ? preview[k] : JSON.stringify(preview[k]);
      return `${label}: ${value}`;
    });
  return lines.length ? lines.join("\n") : JSON.stringify(preview, null, 2);
}
