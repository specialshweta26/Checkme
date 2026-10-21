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
  status: string;
  unread: boolean;
  seen: boolean;
  connector: string;
  connector_slug?: string;
  sort_at?: string;
};

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

  async function refresh(opts: { live?: boolean; quiet?: boolean } = {}) {
    try {
      const res = await fetch(`/api/inbox${opts.live ? "?live=1" : ""}`, { credentials: "include" });
      if (res.status === 401) {
        window.location.replace("/");
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

  if (!me) return <div className="center">Loading your workspace…</div>;

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
