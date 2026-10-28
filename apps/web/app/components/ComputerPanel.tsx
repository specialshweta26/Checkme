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

// Matches the API's boot timeout (routes.BOOT_TIMEOUT).
const BOOT_TIMEOUT_S = 90;

function useLiveScreen(active: boolean) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const held = useRef("");
  useEffect(() => {
    if (!active) return;
    const ac = new AbortController();
    let timer = 0;
    const tick = async () => {
      const request = new AbortController();
      const abortRequest = () => request.abort();
      const timeout = window.setTimeout(() => request.abort(), 12000);
      ac.signal.addEventListener("abort", abortRequest, { once: true });
      try {
        const res = await fetch("/api/connectors/computer/screen", {
          credentials: "include",
          cache: "no-store",
          signal: request.signal,
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body?.detail || `Screen capture failed (${res.status})`);
        }
        const blob = await res.blob();
        if (blob.size < 32) throw new Error("empty screen frame");
        const head = new Uint8Array(await blob.slice(0, 8).arrayBuffer());
        const png = head[0] === 0x89 && head[1] === 0x50 && head[2] === 0x4e && head[3] === 0x47;
        const jpeg = head[0] === 0xff && head[1] === 0xd8;
        const kind = blob.type || res.headers.get("content-type") || "";
        if (!png && !jpeg && !kind.startsWith("image/")) throw new Error("invalid screen frame");
        if (ac.signal.aborted) return;
        const objectUrl = URL.createObjectURL(blob);
        const previous = held.current;
        held.current = objectUrl;
        setUrl(objectUrl);
        setError("");
        if (previous) URL.revokeObjectURL(previous);
      } catch (err: any) {
        if (!ac.signal.aborted) setError(err?.message || "Waiting for a live screen frame");
      } finally {
        ac.signal.removeEventListener("abort", abortRequest);
        window.clearTimeout(timeout);
        if (!ac.signal.aborted) timer = window.setTimeout(tick, 1200);
      }
    };
    void tick();
    return () => {
      ac.abort();
      window.clearTimeout(timer);
    };
  }, [active]);
  useEffect(
    () => () => {
      if (held.current) URL.revokeObjectURL(held.current);
    },
    [],
  );
  return { url, error };
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

async function sendInput(payload: { text?: string; key?: string }) {
  const res = await fetch("/api/connectors/computer/input", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = (await res.json().catch(() => ({}))) as { detail?: string; error?: string };
  if (!res.ok || data.error) throw new Error(String(data.detail || data.error || `Typing failed (${res.status})`).slice(0, 180));
}

const QUICK_KEYS: Array<{ label: string; key: string; title: string }> = [
  { label: "Enter", key: "Enter", title: "Press Enter" },
  { label: "Tab", key: "Tab", title: "Press Tab" },
  { label: "⌫", key: "Backspace", title: "Backspace" },
  { label: "Esc", key: "Escape", title: "Escape" },
  { label: "←", key: "Left", title: "Left arrow" },
  { label: "↑", key: "Up", title: "Up arrow" },
  { label: "↓", key: "Down", title: "Down arrow" },
  { label: "→", key: "Right", title: "Right arrow" },
  { label: "Ctrl+A", key: "ctrl+a", title: "Select all (Ctrl/Cmd+A)" },
  { label: "Ctrl+C", key: "ctrl+c", title: "Copy (Ctrl/Cmd+C)" },
  { label: "Ctrl+V", key: "ctrl+v", title: "Paste the desktop clipboard (Ctrl/Cmd+V)" },
];

/** Phones cannot raise the keyboard inside the noVNC iframe, so text and keys go through the API instead.
 *  Never autofocuses, so desktop typing straight into the iframe keeps working. */
export function ComputerInputBar({ onError }: { onError?: (message: string) => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const run = async (payload: { text?: string; key?: string }) => {
    setBusy(true);
    setError("");
    try {
      await sendInput(payload);
      return true;
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(message);
      onError?.(message);
      return false;
    } finally {
      setBusy(false);
    }
  };
  const submit = async (e?: { preventDefault: () => void }) => {
    e?.preventDefault();
    if (!text || busy) return;
    if (await run({ text })) setText("");
  };
  return (
    <form
      className="screen-input-bar"
      onSubmit={(e) => void submit(e)}
      style={{
        position: "sticky",
        bottom: 0,
        zIndex: 2,
        display: "flex",
        flexDirection: "column",
        gap: 6,
        padding: "8px 8px calc(8px + env(safe-area-inset-bottom))",
        background: "var(--panel, #fff)",
        borderTop: "1px solid var(--line, rgba(0,0,0,0.1))",
      }}
    >
      <div style={{ display: "flex", gap: 6 }}>
        <input
          type="text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Type on the computer…"
          aria-label="Type on the computer"
          enterKeyHint="send"
          autoCapitalize="off"
          autoCorrect="off"
          autoComplete="off"
          spellCheck={false}
          maxLength={2000}
          style={{ flex: 1, minWidth: 0, fontSize: 16, padding: "8px 10px", borderRadius: 8, border: "1px solid var(--line, #ccc)" }}
        />
        <button type="submit" className="btn" disabled={busy || !text} aria-label="Send text to the computer">
          Send
        </button>
      </div>
      <div style={{ display: "flex", gap: 4, overflowX: "auto", WebkitOverflowScrolling: "touch" }}>
        {QUICK_KEYS.map((k) => (
          <button
            key={k.key}
            type="button"
            className="screen-control-btn"
            title={k.title}
            aria-label={k.title}
            disabled={busy}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => void run({ key: k.key })}
            style={{ flex: "0 0 auto" }}
          >
            {k.label}
          </button>
        ))}
      </div>
      {error ? <p className="screen-notice" role="alert">{error}</p> : null}
    </form>
  );
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
  autoBoot = false,
  controlling = false,
  agentName = "Chief",
  status = "",
  teaching = false,
  onTakeControl,
  onRelease,
  onTeachStart,
  onTeachStop,
  onClose,
}: {
  session?: ComputerSession | null;
  watching?: boolean;
  autoBoot?: boolean;
  controlling?: boolean;
  agentName?: string;
  status?: string;
  teaching?: boolean;
  onTakeControl?: () => void;
  onRelease?: () => void;
  onTeachStart?: () => Promise<void>;
  onTeachStop?: (name: string) => Promise<void>;
  onClose?: () => void;
}) {
  const still = useCredentialedImage(session?.preview_url || session?.download_url);
  const [machine, setMachine] = useState<"stopped" | "booting" | "running" | "suspended">("stopped");
  const [pageUrl, setPageUrl] = useState(session?.url || "");
  const [pageTitle, setPageTitle] = useState(session?.title || "");
  const [screenAgent, setScreenAgent] = useState("");
  // E2B desktops expose a live noVNC stream (view-only URL + a control URL). Daytona has none: poll PNG frames.
  const [stream, setStream] = useState<{ view: string; control: string }>({ view: "", control: "" });
  // "" until the first status reply; "e2b" streams over noVNC and never serves PNG frames.
  const [provider, setProvider] = useState("");
  const [bootError, setBootError] = useState("");
  const [bootStarted, setBootStarted] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const autoBooted = useRef(false);
  const liveActive = Boolean(watching || controlling || machine === "running");
  // Teaching records clicks through our own control endpoint, so it keeps the frame view.
  const streaming = Boolean(stream.view) && machine === "running" && !teaching;
  const framesSupported = provider !== "" && provider !== "e2b" && provider !== "none";
  const liveScreen = useLiveScreen(liveActive && !streaming && framesSupported);
  const bootSeconds = machine === "booting" && bootStarted ? Math.max(0, Math.round((now - bootStarted) / 1000)) : 0;
  const bootSlow = bootSeconds >= BOOT_TIMEOUT_S;
  // A saved task preview can be stale or blank; only use it before a live session starts.
  const frame = streaming ? "" : liveScreen.url || (!liveActive ? still : "");
  const href = session?.url || pageUrl || "";
  const asleep = machine !== "running" && machine !== "booting" && !watching && !controlling && !frame;
  const previewRef = useRef<HTMLDivElement>(null);
  const typing = useRef("");
  const typeTimer = useRef(0);
  const actionQueue = useRef<Promise<void>>(Promise.resolve());
  const waking = useRef(false);
  const [teachingBusy, setTeachingBusy] = useState(false);
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
      if (typeof data.provider === "string") setProvider(data.provider);
      if (!waking.current) {
        setMachine(data.state);
        if (data.state === "booting") {
          const since = Date.now() - Number(data.booting_for || 0) * 1000;
          setBootStarted((prev) => prev || since);
        } else {
          setBootStarted(0);
        }
        setBootError(data.state !== "running" && typeof data.error === "string" ? data.error : "");
      }
      applyStream(data);
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
  useEffect(() => {
    if (machine !== "booting") return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [machine]);
  useEffect(() => {
    // The agent is using the computer in this turn but no desktop is up yet: boot it once.
    if (!autoBoot) {
      autoBooted.current = false;
      return;
    }
    if (autoBooted.current || provider !== "e2b" || controlling || teaching) return;
    if (machine !== "stopped" && machine !== "suspended") return;
    autoBooted.current = true;
    void wake();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoBoot, provider, machine, controlling, teaching]);
  let host = "";
  try {
    if (href) host = new URL(href).hostname;
  } catch {
    host = session?.title || pageTitle || "";
  }
  if (/chrome-error/i.test(href)) {
    host = "page failed to load";
  } else if (/chromewebdata/i.test(href) || host === "chromewebdata") {
    host = "loading the page…";
  }
  const label = teaching
    ? "recording task"
    : controlling
      ? "you have control"
      : machine === "booting"
        ? "Starting computer…"
        : watching && machine === "running"
          ? status || "Operating the computer"
          : watching
            ? status || "working"
          : machine === "running"
            ? host || "live"
            : asleep
              ? "suspended"
              : host || status || "idle";

  function applyStream(data: any) {
    const view = typeof data?.stream_url === "string" && data.stream_url.startsWith("https://") ? data.stream_url : "";
    const control = typeof data?.control_url === "string" && data.control_url.startsWith("https://") ? data.control_url : "";
    if (view) setStream((prev) => (prev.view === view && prev.control === control ? prev : { view, control: control || view }));
    else if (data?.state && data.state !== "running") setStream((prev) => (prev.view ? { view: "", control: "" } : prev));
  }

  async function wake() {
    if (waking.current) return false;
    waking.current = true;
    setNotice("");
    setBootError("");
    setMachine("booting");
    setBootStarted(Date.now());
    setNow(Date.now());
    const ac = new AbortController();
    const timer = window.setTimeout(() => ac.abort(), (BOOT_TIMEOUT_S + 5) * 1000);
    try {
      let res: Response;
      try {
        res = await fetch("/api/connectors/computer/boot", { method: "POST", credentials: "include", signal: ac.signal });
      } catch (err: any) {
        if (ac.signal.aborted) throw new Error(`The computer did not start within ${BOOT_TIMEOUT_S} seconds. Try again.`);
        throw err;
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Could not wake the computer (${res.status})`);
      if (typeof data.provider === "string") setProvider(data.provider);
      setMachine("running");
      applyStream(data);
      if (data.url) setPageUrl(data.url);
      if (data.title) setPageTitle(data.title);
      return true;
    } catch (err: any) {
      setMachine("suspended");
      setBootError(err?.message || "Could not wake the computer");
      return false;
    } finally {
      window.clearTimeout(timer);
      setBootStarted(0);
      waking.current = false;
    }
  }


  async function act(payload: Record<string, unknown>) {
    setNotice("");
    const queued = actionQueue.current.then(() => sendControl(payload));
    actionQueue.current = queued.catch((err: any) => {
      setNotice(err?.message || "Control failed");
    });
    await actionQueue.current;
  }

  async function beginTeaching() {
    if (!onTeachStart || teachingBusy) return;
    setTeachingBusy(true);
    try {
      if (machine !== "running" && !(await wake())) return;
      await onTeachStart();
    } catch (err: any) {
      setNotice(err?.message || "Could not start teaching");
    } finally {
      setTeachingBusy(false);
    }
  }

  async function finishTeaching() {
    if (!onTeachStop || teachingBusy) return;
    const name = window.prompt("Name this skill", "Taught workflow");
    if (name === null) return;
    setTeachingBusy(true);
    try {
      await onTeachStop(name.trim() || "Taught workflow");
      setNotice("Skill saved. Your agent can reuse this task.");
      onRelease?.();
    } catch (err: any) {
      setNotice(err?.message || "Could not save the skill");
    } finally {
      setTeachingBusy(false);
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
    <aside className="screen-rail overlay fullscreen" role="dialog" aria-modal="true" aria-label={`${screenAgent || agentName}'s screen`}>
      <div className="screen-rail-head">
        <div className="screen-rail-copy">
          <span className="screen-bot">{`${screenAgent || agentName}'s screen`}</span>
          <span className={`screen-state${controlling || teaching ? " control" : watching ? " live" : asleep ? " asleep" : ""}`}>{label}</span>
        </div>
        <div className="screen-rail-actions">
        {onTeachStart && !teaching ? (
          <button type="button" className="screen-control-btn" disabled={teachingBusy} onClick={() => void beginTeaching()}>
            {teachingBusy ? "Starting…" : "Teach a task"}
          </button>
        ) : null}
        {teaching ? (
          <button type="button" className="screen-control-btn primary" disabled={teachingBusy} onClick={() => void finishTeaching()}>
            {teachingBusy ? "Saving…" : "Stop & save skill"}
          </button>
        ) : null}
        {!controlling && !frame && !streaming && (machine !== "booting" || bootSlow) ? (
          <button type="button" className="screen-control-btn primary" onClick={() => void wake()}>
            Open
          </button>
        ) : null}
        {onTakeControl && !controlling && (Boolean(frame) || streaming || machine === "running") ? (
          <button type="button" className="screen-control-btn" onClick={onTakeControl}>
            Take control
          </button>
        ) : null}
        {controlling && onRelease && !teaching ? (
          <button type="button" className="screen-control-btn primary" onClick={onRelease}>
            Done
          </button>
        ) : null}
        {onClose && !teaching ? (
          <button type="button" className="screen-control-btn" onClick={onClose} aria-label="Close computer">
            Close
          </button>
        ) : null}
        </div>
      </div>
      <div
        ref={previewRef}
        className={`screen-preview fullscreen${asleep ? " asleep" : ""}${controlling ? " controlling" : ""}${streaming ? " streaming" : ""}`}
        tabIndex={controlling && !streaming ? 0 : -1}
        onWheel={
          controlling && !streaming
            ? (e) => {
                e.preventDefault();
                void act({ kind: "scroll", dx: Math.round(e.deltaX), dy: Math.round(e.deltaY) });
              }
            : undefined
        }
        onKeyDown={
          controlling && !streaming
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
        {streaming ? (
          <iframe
            // View-only until the user takes control; noVNC then forwards mouse and keyboard to the desktop.
            key={controlling ? "control" : "view"}
            src={controlling ? stream.control : stream.view}
            title={`${agentName}'s live desktop`}
            className="screen-stream"
            allow="clipboard-read; clipboard-write; fullscreen"
            referrerPolicy="no-referrer"
            style={{ width: "100%", height: "100%", border: 0, display: "block", background: "#111", pointerEvents: controlling ? "auto" : "none" }}
          />
        ) : frame ? (
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
            {bootError && machine !== "running" && machine !== "booting"
              ? `Computer failed to start: ${bootError}`
              : machine === "booting"
                ? bootSlow
                  ? `Still starting after ${bootSeconds}s. The desktop provider is slow or stuck; it may still come up, or click Open to retry.`
                  : `Starting computer… ${bootSeconds ? `${bootSeconds}s` : ""} The screen appears as soon as the desktop is up.`
                : machine === "running"
                  ? liveScreen.error && !watching
                    ? `Live screen unavailable. Retrying… ${liveScreen.error}`
                    : "Connecting to the live desktop…"
                  : controlling
                    ? "Connecting to the live desktop…"
                    : watching
                      ? `${agentName} has not opened the computer in this task. Click Open to start it.`
                      : "Computer is asleep. Open it to wake."}
          </p>
        )}
      </div>
      <p className="screen-caption">
        {teaching
          ? "Recording your clicks, typing, and scrolling. No audio is recorded."
          : controlling
            ? session?.handoff_reason || "Click, type, and scroll on this screen."
            : streaming
              ? `${agentName}'s screen (live, view only). Take control to use it.`
            : `${agentName}'s screen`}
      </p>
      {notice ? <p className="screen-notice">{notice}</p> : null}
      {!teaching && (controlling || streaming || machine === "running") ? <ComputerInputBar /> : null}
    </aside>
  );
}
