"use client";

import { useEffect, useState } from "react";

export type Attachment = {
  filename: string;
  url?: string;
  download_url?: string;
  preview_url?: string;
  content_type?: string;
  kind?: string;
  previewable?: boolean;
  message_id?: string;
  attachment_id?: string;
  connector_slug?: string;
  error?: string;
};

function IconEye() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function IconDownload() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M12 3v12" />
      <path d="m7 11 5 5 5-5" />
      <path d="M5 21h14" />
    </svg>
  );
}

function IconSpark() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M12 3v6M12 15v6M3 12h6M15 12h6" />
      <path d="m6 6 3 3M15 15l3 3M6 18l3-3M15 9l3-3" />
    </svg>
  );
}

function kindOf(a: Attachment): string {
  if (a.kind) return a.kind;
  if (a.content_type?.startsWith("image/")) return "image";
  if (a.content_type?.includes("pdf") || a.filename?.toLowerCase().endsWith(".pdf")) return "pdf";
  return "file";
}

export function AttachmentWidget({ items }: { items: Attachment[] }) {
  if (!items.length) return null;
  return (
    <div className="att-list">
      {items.map((a, i) => (
        <AttachmentRow key={`${a.filename}-${a.attachment_id || i}`} item={a} />
      ))}
    </div>
  );
}

function useObjectUrl(src: string | undefined, enabled: boolean) {
  const [url, setUrl] = useState("");
  const [mime, setMime] = useState("");
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!enabled || !src) return;
    const ac = new AbortController();
    let objectUrl = "";
    setLoading(true);
    setLoadError("");
    setUrl("");
    setMime("");
    (async () => {
      const res = await fetch(src, { credentials: "include", cache: "no-store", signal: ac.signal });
      if (!res.ok) throw new Error("Could not load preview");
      const blob = await res.blob();
      if (ac.signal.aborted) return;
      objectUrl = URL.createObjectURL(blob);
      setUrl(objectUrl);
      setMime(blob.type || "");
    })()
      .catch((err) => {
        if (!ac.signal.aborted) setLoadError(err.message || "Could not load preview");
      })
      .finally(() => {
        if (!ac.signal.aborted) setLoading(false);
      });
    return () => {
      ac.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [src, enabled]);

  return { url, mime, loadError, loading };
}

function AttachmentRow({ item }: { item: Attachment }) {
  const [open, setOpen] = useState(false);
  const [summary, setSummary] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const download = item.download_url || item.url;
  const preview = item.preview_url || download;
  const kind = kindOf(item);
  const pdfRender = preview && (kind === "pdf" || (preview || "").toLowerCase().includes(".pdf"))
    ? `${preview}${preview.includes("?") ? "&" : "?"}render=1`
    : "";
  const { url: previewSrc, mime, loadError, loading } = useObjectUrl(
    kind === "pdf" ? pdfRender || preview : preview,
    open && (kind === "image" || kind === "pdf"),
  );
  const showImage = !pdfRender && (kind === "image" || mime.startsWith("image/"));

  async function summarize() {
    if (!item.message_id || !item.attachment_id || !item.connector_slug) {
      setError("This file is missing ids needed to summarize.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/chat/attachments/summarize", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message_id: item.message_id,
          attachment_id: item.attachment_id,
          connector_slug: item.connector_slug,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Summarize failed");
      setSummary(data.summary || "No summary.");
    } catch (err: any) {
      setError(err.message || "Summarize failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="att-widget">
      <div className="att-name">{item.filename || "Attachment"}</div>
      {item.error ? <p className="err">{item.error}</p> : null}
      <div className="att-actions">
        <button type="button" className={`att-btn${open ? " on" : ""}`} onClick={() => setOpen((v) => !v)} disabled={!preview}>
          <IconEye /> {open ? "Hide preview" : "Preview"}
        </button>
        <a className="att-btn" href={download || undefined} download={item.filename} target="_blank" rel="noreferrer">
          <IconDownload /> Download file
        </a>
        <button type="button" className="att-btn" onClick={summarize} disabled={busy}>
          <IconSpark /> {busy ? "Summarizing…" : "Summarize"}
        </button>
      </div>
      {open && preview ? (
        <div className={`att-preview${pdfRender ? " pdf" : ""}`}>
          {loading ? <p className="muted att-preview-status">Loading preview…</p> : null}
          {loadError ? (
            <p className="err att-preview-status">
              {loadError}.{" "}
              <a href={preview} target="_blank" rel="noreferrer">
                Open file
              </a>
            </p>
          ) : null}
          {previewSrc && (pdfRender || showImage) ? (
            <div className="att-preview-scroll">
              <img src={previewSrc} alt={item.filename || "Attachment preview"} />
            </div>
          ) : null}
          {!pdfRender && !showImage && !loading ? (
            <a className="muted att-preview-status" href={preview} target="_blank" rel="noreferrer">
              Open preview
            </a>
          ) : null}
        </div>
      ) : null}
      {summary ? <div className="att-summary">{summary}</div> : null}
      {error ? <p className="err">{error}</p> : null}
    </div>
  );
}
