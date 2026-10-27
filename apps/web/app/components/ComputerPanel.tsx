"use client";

import { useEffect, useRef, useState, type MouseEvent } from "react";

export type ComputerSession = {
  live?: boolean;
  url?: string;
  title?: string;
  preview_url?: string;
  download_url?: string;
  handoff?: boolean;
  handoff_reason?: string;
};

export function isScreenAttachment(row: any): boolean {
  if (!row) return false;
  const name = String(row.filename || "").toLowerCase();
  return row.kind === "screen" || name === "page.png" || /^page\.(png|jpe?g|webp)$/i.test(name);
}

export function chatAttachments(items: any[] | undefined | null) {
  return (items || []).filter((row) => !isScreenAttachment(row));
}

function useCredentialedImage(src: string | undefined) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    if (!src) {
      setUrl("");
      return;
    }
    if (/^https?:\/\//i.test(src) && !src.startsWith(window.location.origin)) {
      setUrl(src);
      return;
    }
    const ac = new AbortController();
    let objectUrl = "";
    (async () => {
      const res = await fetch(src, { credentials: "include", cache: "no-store", signal: ac.signal });
      if (!res.ok) throw new Error("preview failed");
      const blob = await res.blob();
      if (!blob.type.startsWith("image/")) throw new Error("not an image");
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

function useLiveScreen(active: boolean) {
  const [url, setUrl] = useState("");
  const held = useRef("");
  useEffect(() => {
    if (!active) return;
    let timer = 0;
    const ac = new AbortController();
    const tick = async () => {
      try {
        const res = await fetch("/api/connectors/computer/screen", {
          credentials: "include",
          cache: "no-store",
          signal: ac.signal,
        });
        if (!res.ok) return;
        const blob = await res.blob();
        if (blob.size < 32) return;
        const head = new Uint8Array(await blob.slice(0, 8).arrayBuffer());
        const png = head[0] === 0x89 && head[1] === 0x50 && head[2] === 0x4e && head[3] === 0x47;
        const jpeg = head[0] === 0xff && head[1] === 0xd8;
        const kind = blob.type || res.headers.get("content-type") || "";
        if (!png && !jpeg && kind && !kind.startsWith("image/")) return;
        if (held.current) URL.revokeObjectURL(held.current);
        const objectUrl = URL.createObjectURL(blob);
        held.current = objectUrl;
        setUrl(objectUrl);
      } catch {
        /* keep last frame */
      }
    };
    tick();
    timer = window.setInterval(tick, 1500);
    return () => {
      ac.abort();
      window.clearInterval(timer);
    };
  }, [active]);
  useEffect(
    () => () => {
      if (held.current) URL.revokeObjectURL(held.current);
    },
    [],
  );
  return url;
}

export function computerFromMeta(meta: any): ComputerSession | null {
  const given = meta?.computer && typeof meta.computer === "object" ? meta.computer : {};
  const views = Array.isArray(meta?.browser) ? meta.browser : [];
  const shot = views.find((row: any) => row?.preview_url || row?.url) || {};
  const used = Array.isArray(meta?.used_tools) ? meta.used_tools.join(" ") : "";
  const browsed = /browser_|browse_|computer_observe|computer_act|handoff/.test(used);
  const atts = Array.isArray(meta?.attachments) ? meta.attachments : [];
  const att = atts.find(isScreenAttachment) || atts.find((row: any) => row?.preview_url && (row.kind === "image" || /\.(png|jpe?g|webp)$/i.test(row.filename || "")));
  const url = String(given.url || shot.url || "");
  const preview = String(given.preview_url || shot.preview_url || att?.preview_url || "");
  const download = String(given.download_url || shot.download_url || att?.download_url || "");
  const live = Boolean(given.live || given.handoff || preview || url || browsed);
  if (!live) return null;
  return {
    live,
    url,
    title: String(given.title || shot.title || ""),
    preview_url: preview,
    download_url: download,
    handoff: Boolean(given.handoff),
    handoff_reason: String(given.handoff_reason || ""),
  };
}

function mapClick(e: MouseEvent<HTMLImageElement>, img: HTMLImageElement) {
  const rect = img.getBoundingClientRect();
  const nw = img.naturalWidth || 1280;
  const nh = img.naturalHeight || 800;
  const scale = Math.min(rect.width / nw, rect.height / nh);
  const dw = nw * scale;
  const dh = nh * scale;
  const ox = rect.left + (rect.width - dw) / 2;
  const oy = rect.top + (rect.height - dh) / 2;
  const x = Math.round(((e.clientX - ox) / dw) * nw);
  const y = Math.round(((e.clientY - oy) / dh) * nh);
  return { x: Math.max(0, Math.min(nw - 1, x)), y: Math.max(0, Math.min(nh - 1, y)) };
}

async function sendControl(payload: Record<string, unknown>) {
  const res = await fetch("/api/connectors/computer/control", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text.slice(0, 180) || "Control failed");
  }
}

export function ComputerIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="3.5" y="4.5" width="17" height="12" rx="2" stroke="currentColor" strokeWidth="1.8" />
      <path d="M8 20h8M12 16.5V20" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

export function ScreenRail({
  session,
  watching = false,
  controlling = false,
  agentName = "Chief",
  status = "",
  onTakeControl,
  onRelease,
  onClose,
}: {
  session?: ComputerSession | null;
  watching?: boolean;
  controlling?: boolean;
  agentName?: string;
  status?: string;
  onTakeControl?: () => void;
  onRelease?: () => void;
  onClose?: () => void;
}) {
  const still = useCredentialedImage(session?.preview_url || session?.download_url);
  const [machine, setMachine] = useState<"stopped" | "booting" | "running" | "suspended">("stopped");
  const [pageUrl, setPageUrl] = useState(session?.url || "");
  const [pageTitle, setPageTitle] = useState(session?.title || "");
  const [screenAgent, setScreenAgent] = useState("");
  const liveFrame = useLiveScreen(Boolean(watching || controlling || machine === "running"));
  const frame = liveFrame || still;
  const href = session?.url || pageUrl || "";
  const asleep = machine !== "running" && machine !== "booting" && !watching && !controlling && !frame;
  const previewRef = useRef<HTMLDivElement>(null);
  const typing = useRef("");
  const typeTimer = useRef(0);
  const waking = useRef(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    if (controlling) previewRef.current?.focus();
  }, [controlling]);
  useEffect(() => {
    if (session?.url) setPageUrl(session.url);
    if (session?.title) setPageTitle(session.title);
  }, [session?.url, session?.title]);
  useEffect(() => {
    const ac = new AbortController();
    const apply = (data: any) => {
      if (!data?.state) return;
      setMachine(data.state);
      if (data.url) setPageUrl(data.url);
      if (data.title) setPageTitle(data.title);
      if (typeof data.screen_agent === "string") setScreenAgent(data.screen_agent);
    };
    const tick = () => {
      fetch("/api/connectors/computer/status", { credentials: "include", signal: ac.signal })
        .then((res) => (res.ok ? res.json() : null))
        .then(apply)
        .catch(() => undefined);
    };
    tick();
    const timer = window.setInterval(tick, 2500);
    return () => {
      ac.abort();
      window.clearInterval(timer);
    };
  }, []);
  let host = "";
  try {
    if (href) host = new URL(href).hostname;
  } catch {
    host = session?.title || pageTitle || "";
  }
  if (/chromewebdata|chrome-error/i.test(href) || host === "chromewebdata") {
    host = "loading the page…";
  }
  const label = controlling
    ? "you have control"
    : machine === "booting"
      ? "Booting live desktop…"
      : watching
        ? status || "Operating the computer"
        : machine === "running"
          ? host || "live"
          : asleep
            ? "suspended"
            : host || status || "idle";

  async function wake() {
    if (waking.current) return;
    waking.current = true;
    setNotice("");
    setMachine("booting");
    try {
      const res = await fetch("/api/connectors/computer/boot", { method: "POST", credentials: "include" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not wake the computer");
      setMachine("running");
      if (data.url) setPageUrl(data.url);
      if (data.title) setPageTitle(data.title);
    } catch (err: any) {
      setMachine("suspended");
      setNotice(err?.message || "Could not wake the computer");
    } finally {
      waking.current = false;
    }
  }


  async function act(payload: Record<string, unknown>) {
    setNotice("");
    try {
      await sendControl(payload);
    } catch (err: any) {
      setNotice(err?.message || "Control failed");
    }
  }

  function flushType() {
    const text = typing.current;
    typing.current = "";
    if (typeTimer.current) window.clearTimeout(typeTimer.current);
    typeTimer.current = 0;
    if (text) void act({ kind: "type", text });
  }

  return (
    <aside className="screen-rail overlay" aria-label={`${screenAgent || agentName}'s screen`}>
      <div className="screen-rail-head">
        <div className="screen-rail-copy">
          <span className="screen-bot">{`${screenAgent || agentName}'s screen`}</span>
          <span className={`screen-state${controlling ? " control" : watching ? " live" : asleep ? " asleep" : ""}`}>{label}</span>
        </div>
        <div className="screen-rail-actions">
        {!controlling && !frame && machine !== "booting" ? (
          <button type="button" className="screen-control-btn primary" onClick={() => void wake()}>
            Open
          </button>
        ) : null}
        {onTakeControl && !controlling && (Boolean(frame) || machine === "running") ? (
          <button type="button" className="screen-control-btn" onClick={onTakeControl}>
            Take control
          </button>
        ) : null}
        {controlling && onRelease ? (
          <button type="button" className="screen-control-btn primary" onClick={onRelease}>
            Done
          </button>
        ) : null}
        {onClose ? (
          <button type="button" className="screen-control-btn" onClick={onClose} aria-label="Close computer">
            Close
          </button>
        ) : null}
        </div>
      </div>
      <div
        ref={previewRef}
        className={`screen-preview${asleep ? " asleep" : ""}${controlling ? " controlling" : ""}`}
        tabIndex={controlling ? 0 : -1}
        onWheel={
          controlling
            ? (e) => {
                e.preventDefault();
                void act({ kind: "scroll", dx: Math.round(e.deltaX), dy: Math.round(e.deltaY) });
              }
            : undefined
        }
        onKeyDown={
          controlling
            ? (e) => {
                if (e.metaKey || e.ctrlKey) {
                  if (e.key === "a" || e.key === "A") {
                    e.preventDefault();
                    flushType();
                    void act({ kind: "key", key: e.metaKey ? "Meta+A" : "Control+A" });
                    return;
                  }
                  if (e.key === "v" || e.key === "V") return;
                }
                if (e.key === "Tab" || e.key === "Enter" || e.key === "Escape" || e.key === "Backspace") {
                  e.preventDefault();
                  flushType();
                  void act({ kind: "key", key: e.key });
                  return;
                }
                if (e.key.length === 1 && !e.metaKey && !e.ctrlKey) {
                  e.preventDefault();
                  typing.current += e.key;
                  if (typeTimer.current) window.clearTimeout(typeTimer.current);
                  typeTimer.current = window.setTimeout(flushType, 280);
                }
              }
            : undefined
        }
      >
        {frame ? (
          <img
            src={frame}
            alt={`${agentName}'s screen`}
            onClick={
              controlling
                ? (e) => {
                    flushType();
                    const point = mapClick(e, e.currentTarget);
                    void act({ kind: "click", x: point.x, y: point.y });
                    previewRef.current?.focus();
                  }
                : undefined
            }
          />
        ) : (
          <p className="screen-placeholder">
            {machine === "booting"
              ? "Booting live desktop…"
              : watching || controlling || machine === "running"
                ? "Opening the live desktop…"
                : "Computer is asleep. Open it to wake."}
          </p>
        )}
      </div>
      <p className="screen-caption">
        {controlling ? session?.handoff_reason || "Click, type, and scroll on this screen." : `${agentName}'s screen`}
      </p>
      {notice ? <p className="screen-notice">{notice}</p> : null}
    </aside>
  );
}
