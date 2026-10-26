"use client";

import { useEffect, useState } from "react";

export type BrowserView = {
  url?: string;
  title?: string;
  preview_url?: string;
  download_url?: string;
  error?: string;
};

function useShot(src: string | undefined) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    if (!src) return;
    const ac = new AbortController();
    let objectUrl = "";
    (async () => {
      const res = await fetch(src, { credentials: "include", cache: "no-store", signal: ac.signal });
      if (!res.ok) throw new Error("preview failed");
      const blob = await res.blob();
      objectUrl = URL.createObjectURL(blob);
      if (!ac.signal.aborted) setUrl(objectUrl);
    })().catch(() => undefined);
    return () => {
      ac.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [src]);
  return url;
}

function BrowserPanel({ view }: { view: BrowserView }) {
  const shot = useShot(view.preview_url);
  const href = view.url || view.download_url || view.preview_url || "";
  const host = (() => {
    try {
      return view.url ? new URL(view.url).hostname : "browser";
    } catch {
      return "browser";
    }
  })();

  return (
    <div className="browser-frame">
      <div className="browser-chrome">
        <span className="browser-dots" aria-hidden>
          <i />
          <i />
          <i />
        </span>
        <div className="browser-url">{view.url || host}</div>
        {href ? (
          <a className="browser-open" href={href} target="_blank" rel="noreferrer">
            Open tab
          </a>
        ) : null}
      </div>
      {view.error ? <p className="browser-note">{view.error}</p> : null}
      <div className="browser-stage">
        {shot ? (
          <img src={shot} alt={view.title || host} />
        ) : view.url ? (
          <iframe className="browser-iframe" title={view.title || host} src={view.url} sandbox="allow-scripts allow-same-origin allow-forms allow-popups" />
        ) : (
          <p className="muted browser-empty">Loading page…</p>
        )}
      </div>
    </div>
  );
}

export function BrowserFrames({ views }: { views: BrowserView[] }) {
  const usable = (views || []).filter((v) => v && (v.url || v.preview_url));
  if (!usable.length) return null;
  return (
    <div className="browser-list">
      {usable.map((view, i) => (
        <BrowserPanel key={`${view.url || view.preview_url}-${i}`} view={view} />
      ))}
    </div>
  );
}
