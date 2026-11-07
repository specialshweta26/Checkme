"use client";

import { useEffect, useMemo, useState } from "react";
import { AppShell } from "../components/AppShell";
import { EmailThreadButton } from "../components/EmailThread";
import { getMe, uploadFiles, type Me, type UploadedFile } from "../lib";

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
  key_person?: boolean;
  edited?: boolean;
  sender_note?: string;
  status: string;
  unread: boolean;
  seen: boolean;
  connector: string;
  connector_slug?: string;
  sort_at?: string;
  to?: string;
  cc?: string;
  recipient_role?: string;
};

type CopyFields = { cc: string; bcc: string; replyAll: boolean };

function parseEmails(raw: string): string[] {
  return raw
    .split(/[\s,;]+/)
    .map((part) => part.trim())
    .filter((part) => part.includes("@"));
}

const TONES = ["Professional", "Concise", "Friendly", "Warmer"] as const;

const BATCH = [
  { id: "empty_spam", label: "Empty spam" },
  { id: "empty_drafts", label: "Clear drafts" },
  { id: "empty_trash", label: "Empty trash" },
] as const;

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
  const [confirm, setConfirm] = useState<{ action: string; title: string; count: number; sample: string; itemIds: string[] } | null>(null);
  const [filesById, setFilesById] = useState<Record<string, UploadedFile[]>>({});
  const [copyById, setCopyById] = useState<Record<string, CopyFields>>({});

  useEffect(() => {
    getMe().then((u) => {
      if (!u) {
        window.location.replace("/login");
        return;
      }
      setMe(u);
    });
    setFocusId(new URLSearchParams(window.location.search).get("mail") || "");
  }, []);

  async function refresh(opts: { live?: boolean; quiet?: boolean } = {}) {
    try {
      const res = await fetch(`/api/inbox${opts.live ? "?live=1" : ""}`, { credentials: "include" });
      if (res.status === 401) {
        window.location.replace("/login");
        return;
      }
      if (!res.ok) {
        const text = await res.text();
        if (!opts.quiet) setError(text || "Could not load inbox");
        setLoading(false);
        return;
      }
      const data = await res.json();
      setItems(data.items || []);
      setConnected(data.connected !== false);
      setError("");
      setLoading(false);
    } catch {
      if (!opts.quiet) setError("Could not load inbox");
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!me) return;
    refresh({ live: true });
    function onLive(ev: Event) {
      const detail = (ev as CustomEvent).detail || {};
      if (Array.isArray(detail.inbox)) {
        setItems(detail.inbox);
        setConnected(detail.connected !== false);
        setLoading(false);
      }
    }
    window.addEventListener("inbox-live", onLive);
    return () => window.removeEventListener("inbox-live", onLive);
  }, [me]);

  useEffect(() => {
    if (!items.some((m) => !m.classification && m.status === "pending")) return;
    const id = window.setTimeout(() => refresh(), 2500);
    return () => window.clearTimeout(id);
  }, [items]);

  const visible = useMemo(() => {
    const rows = filter === "all" ? items : items.filter((m) => m.classification === filter);
    return [...rows].sort((a, b) => {
      // The user's key people first (from memory), then newest first.
      const key = Number(!!b.key_person && b.status === "pending") - Number(!!a.key_person && a.status === "pending");
      if (key) return key;
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

  // After Rephrase / Redraft: what happened, and the draft before it, so the change is visible and reversible.
  const [changed, setChanged] = useState<Record<string, { kind: "rephrase" | "redraft"; prev: string }>>({});

  function update(id: string, patch: Partial<InboxMail>) {
    setItems((rows) => rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  async function act(id: string, path: string, body?: Record<string, unknown>) {
    setBusy(`${id}:${path}`);
    setError("");
    const item = items.find((m) => m.id === id);
    const rewrite = path === "/redraft" || path === "/rephrase" ? (path === "/redraft" ? "redraft" : "rephrase") : null;
    if (rewrite) setChanged((cur) => { const next = { ...cur }; delete next[id]; return next; });
    const res = await fetch(`/api/inbox/${id}${path}`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        draft: item?.draft || "",
        attachment_keys: (filesById[id] || []).map((f) => f.key),
        ...body,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setError(typeof data.detail === "string" ? data.detail : "That action failed");
      setBusy("");
      return;
    }
    if (path === "/send" || path === "/delete") {
      setItems((rows) => rows.filter((r) => r.id !== id));
      setFilesById((cur) => {
        const next = { ...cur };
        delete next[id];
        return next;
      });
    } else if (data.draft || data.classification) {
      update(id, data);
    } else if (data.item) {
      update(id, data.item);
    }
    if (rewrite && item) setChanged((cur) => ({ ...cur, [id]: { kind: rewrite, prev: item.draft } }));
    setBusy("");
    setOpenRephrase("");
  }

  function sampleText(sample: unknown): string {
    if (!Array.isArray(sample) || sample.length === 0) return "";
    return sample
      .map((row) => {
        if (!row || typeof row !== "object") return String(row);
        const item = row as { subject?: string; from?: string };
        return [item.subject || "(no subject)", item.from].filter(Boolean).join(" — ");
      })
      .join("\n");
  }

  async function askBatch(action: string, title: string, itemIds: string[] = []) {
    setBusy(action);
    setError("");
    try {
      const res = await fetch("/api/inbox/batch", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, confirm: false, item_ids: itemIds }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(typeof data.detail === "string" ? data.detail : "That batch action failed");
        setBusy("");
        return;
      }
      const count = Number(data.count || 0);
      if (!count) {
        setError("Nothing to clear.");
        setBusy("");
        return;
      }
      setConfirm({ action, title, count, sample: sampleText(data.sample), itemIds });
    } catch (err: any) {
      setError(err.message || "That batch action failed");
    }
    setBusy("");
  }

  async function runBatch() {
    if (!confirm) return;
    setBusy(confirm.action);
    try {
      const res = await fetch("/api/inbox/batch", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: confirm.action, confirm: true, item_ids: confirm.itemIds }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(typeof data.detail === "string" ? data.detail : "That batch action failed");
        setBusy("");
        return;
      }
      setConfirm(null);
      await refresh({ live: true });
    } catch (err: any) {
      setError(err.message || "That batch action failed");
    }
    setBusy("");
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

  return (
    <AppShell me={me} current="inbox">
      <div className="page inbox-page">
        <div className="page-hero">
          <div>
            <h1>Inbox</h1>
            <p className="muted">Triage arriving mail, review the thread, and send a drafted reply.</p>
          </div>
          <button className="btn secondary" type="button" onClick={() => { setLoading(true); refresh({ live: true }); }} disabled={loading || Boolean(busy)}>
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

        {connected ? (
          <div className="inbox-batch">
            {BATCH.map((row) => (
              <button key={row.id} className="chip" type="button" disabled={Boolean(busy)} onClick={() => askBatch(row.id, row.label)}>
                {row.label}
              </button>
            ))}
            {items.some((m) => m.classification === "discard") ? (
              <button className="chip" type="button" disabled={Boolean(busy)} onClick={() => askBatch("clear_discard", "Clear discard cards")}>
                Clear discard cards
              </button>
            ) : null}
            {visible.length > 1 ? (
              <button
                className="chip"
                type="button"
                disabled={Boolean(busy)}
                onClick={() => askBatch("delete_items", "Delete messages in this view", visible.map((m) => m.id))}
              >
                Delete all in view
              </button>
            ) : null}
          </div>
        ) : null}

        {confirm ? (
          <div className="inbox-confirm">
            <strong>{confirm.title}</strong>
            <p className="muted">
              {confirm.count} item{confirm.count === 1 ? "" : "s"} will be removed. This cannot be undone from the app.
            </p>
            {confirm.sample ? <pre className="approval-preview">{confirm.sample}</pre> : null}
            <div className="inbox-actions">
              <button className="send-btn" type="button" disabled={Boolean(busy)} onClick={runBatch}>
                Confirm
              </button>
              <button className="btn secondary" type="button" disabled={Boolean(busy)} onClick={() => setConfirm(null)}>
                Cancel
              </button>
            </div>
          </div>
        ) : null}

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
                    <span className="inbox-card-flags">
                      {mail.key_person ? <span className="role-tag">Key contact</span> : null}
                      {mail.recipient_role === "cc" || mail.recipient_role === "bcc" ? (
                        <span className="role-tag">{mail.recipient_role === "bcc" ? "Bcc’d to you" : "Cc’d to you"}</span>
                      ) : null}
                      <span className={`status-dot ${mail.status === "pending" ? "active" : "ok"}`}>
                        {mail.status === "pending" ? "Pending" : "Completed"}
                      </span>
                    </span>
                  </header>
                  <h2>{mail.subject || "(no subject)"}</h2>
                  <p className="inbox-meta">
                    {mail.from || "Unknown sender"}
                    {mail.date ? ` · ${mail.date}` : ""}
                    {mail.connector ? ` · ${mail.connector}` : ""}
                    {mail.cc ? ` · Cc ${mail.cc}` : ""}
                  </p>
                  {mail.sender_note ? <p className="faint">{mail.sender_note}</p> : null}
                  {mail.summary ? <p className="inbox-summary">{mail.summary}</p> : <p className="muted">{mail.snippet || "Reading this thread…"}</p>}
                  {mail.key_points?.length ? (
                    <ul className="inbox-points">
                      {mail.key_points.map((point) => (
                        <li key={point}>{point}</li>
                      ))}
                    </ul>
                  ) : null}
                  <EmailThreadButton
                    refs={[
                      {
                        item_id: mail.id,
                        connector_slug: mail.connector_slug,
                        thread_id: mail.thread_id,
                        message_id: mail.message_id,
                        subject: mail.subject,
                      },
                    ]}
                  />
                  {busy === `${mail.id}:/redraft` ? (
                    <p className="inbox-working">Reading the whole thread again and writing a new summary and draft… (about 5–10 seconds)</p>
                  ) : busy === `${mail.id}:/rephrase` ? (
                    <p className="inbox-working">Rewording your draft…</p>
                  ) : changed[mail.id] ? (
                    <div className="inbox-changed">
                      <span>
                        {changed[mail.id].kind === "redraft"
                          ? changed[mail.id].prev === mail.draft
                            ? "Redrafted: the summary was refreshed; the draft came out the same."
                            : "Redrafted: new summary, key points and draft below."
                          : "Reworded: your draft below has the new wording."}
                      </span>
                      {changed[mail.id].prev && changed[mail.id].prev !== mail.draft ? (
                        <button
                          type="button"
                          className="chip"
                          onClick={() => {
                            const prev = changed[mail.id].prev;
                            update(mail.id, { draft: prev });
                            saveDraft(mail.id, prev);
                            setChanged((cur) => { const next = { ...cur }; delete next[mail.id]; return next; });
                          }}
                        >
                          Use previous draft
                        </button>
                      ) : null}
                      <button type="button" className="chip" onClick={() => setChanged((cur) => { const next = { ...cur }; delete next[mail.id]; return next; })}>
                        OK
                      </button>
                    </div>
                  ) : null}
                  <label className="inbox-draft-label">Drafted reply</label>
                  <textarea
                    className={`inbox-draft${changed[mail.id] ? " fresh" : ""}`}
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
                  {(filesById[mail.id] || []).length ? (
                    <div className="attach-chips">
                      {(filesById[mail.id] || []).map((file) => (
                        <span key={file.key} className="attach-chip">
                          {file.filename}
                          <button type="button" aria-label="Remove file" onClick={() => setFilesById((cur) => ({ ...cur, [mail.id]: (cur[mail.id] || []).filter((f) => f.key !== file.key) }))}>
                            ×
                          </button>
                        </span>
                      ))}
                    </div>
                  ) : null}
                  <div className="inbox-copy">
                    <label>
                      Cc
                      <input
                        value={(copyById[mail.id] || { cc: "", bcc: "", replyAll: false }).cc}
                        onChange={(e) =>
                          setCopyById((cur) => ({
                            ...cur,
                            [mail.id]: { ...(cur[mail.id] || { cc: "", bcc: "", replyAll: false }), cc: e.target.value },
                          }))
                        }
                        placeholder="other@company.com"
                        disabled={working}
                      />
                    </label>
                    <label>
                      Bcc
                      <input
                        value={(copyById[mail.id] || { cc: "", bcc: "", replyAll: false }).bcc}
                        onChange={(e) =>
                          setCopyById((cur) => ({
                            ...cur,
                            [mail.id]: { ...(cur[mail.id] || { cc: "", bcc: "", replyAll: false }), bcc: e.target.value },
                          }))
                        }
                        placeholder="hidden@company.com"
                        disabled={working}
                      />
                    </label>
                    <label className="inbox-reply-all">
                      <input
                        type="checkbox"
                        checked={(copyById[mail.id] || { cc: "", bcc: "", replyAll: false }).replyAll}
                        onChange={(e) =>
                          setCopyById((cur) => ({
                            ...cur,
                            [mail.id]: { ...(cur[mail.id] || { cc: "", bcc: "", replyAll: false }), replyAll: e.target.checked },
                          }))
                        }
                        disabled={working}
                      />
                      Reply all
                    </label>
                  </div>
                  <footer className="inbox-actions">
                    <label className="rephrase-btn attach-btn">
                      Attach
                      <input
                        type="file"
                        multiple
                        hidden
                        disabled={working}
                        onChange={async (e) => {
                          const picked = Array.from(e.target.files || []);
                          e.target.value = "";
                          if (!picked.length) return;
                          try {
                            const uploaded = await uploadFiles(picked);
                            setFilesById((cur) => ({ ...cur, [mail.id]: [...(cur[mail.id] || []), ...uploaded] }));
                          } catch (err: any) {
                            setError(err.message || "Upload failed");
                          }
                        }}
                      />
                    </label>
                    <button
                      className="rephrase-btn"
                      type="button"
                      title="Keep what the draft says and change how it says it: shorter, more formal, or your own instruction"
                      disabled={working}
                      onClick={() => setOpenRephrase(openRephrase === mail.id ? "" : mail.id)}
                    >
                      {busy === `${mail.id}:/rephrase` ? "Rephrasing…" : "✦ Rephrase"}
                    </button>
                    <button
                      className="rephrase-btn"
                      type="button"
                      title="Read the thread again and redo the summary and draft with what your assistant knows now"
                      disabled={working}
                      onClick={() => {
                        if (mail.edited && !window.confirm("Replace your edited draft with a new one?")) return;
                        act(mail.id, "/redraft", {});
                      }}
                    >
                      {busy === `${mail.id}:/redraft` ? "Redrafting…" : "↻ Redraft"}
                    </button>
                    <button
                      className="send-btn"
                      type="button"
                      disabled={working || !mail.draft.trim()}
                      onClick={() => {
                        const copy = copyById[mail.id] || { cc: "", bcc: "", replyAll: false };
                        act(mail.id, "/send", {
                          cc: parseEmails(copy.cc),
                          bcc: parseEmails(copy.bcc),
                          reply_all: copy.replyAll,
                        });
                      }}
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
