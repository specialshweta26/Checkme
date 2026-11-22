"use client";

import { useEffect, useRef, useState } from "react";
import { isLumioDesktop } from "../lib";

// The chat's microphone (apps/api/app/voice/routes.py, docs/VOICE.md). Press to record, press again to stop; the words
// land in the message box for the user to read and edit before sending. Stops by itself after two minutes.

const MAX_MS = 120_000;

function pickType(): string {
  const types = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"];
  try {
    return types.find((t) => typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(t)) || "";
  } catch {
    return "";
  }
}

export function VoiceButton({ onText, disabled }: { onText: (text: string) => void; disabled?: boolean }) {
  const [enabled, setEnabled] = useState(false);
  const [state, setState] = useState<"idle" | "recording" | "working">("idle");
  const [error, setError] = useState("");
  const rec = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const started = useRef(0);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    // The desktop app's window can't ask macOS for the microphone until its bundle declares it (a desktop release), so
    // the button is web-only for now.
    const supported =
      typeof window !== "undefined" && !isLumioDesktop() && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== "undefined";
    if (!supported) return;
    fetch("/api/voice/status", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : { enabled: false }))
      .then((d) => setEnabled(Boolean(d.enabled)))
      .catch(() => undefined);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
      rec.current?.stream.getTracks().forEach((t) => t.stop());
    };
  }, []);

  async function start() {
    setError("");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const type = pickType();
      const r = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      chunks.current = [];
      r.ondataavailable = (e) => e.data.size && chunks.current.push(e.data);
      r.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        void upload(r.mimeType || type || "audio/webm");
      };
      rec.current = r;
      started.current = Date.now();
      r.start();
      setState("recording");
      timer.current = window.setTimeout(stop, MAX_MS);
    } catch {
      setError("Allow the microphone to use voice input.");
    }
  }

  function stop() {
    if (timer.current) window.clearTimeout(timer.current);
    if (rec.current && rec.current.state !== "inactive") rec.current.stop();
  }

  async function upload(type: string) {
    setState("working");
    const blob = new Blob(chunks.current, { type: type.split(";")[0] });
    const form = new FormData();
    form.append("audio", blob, "voice");
    form.append("seconds", String(Math.round((Date.now() - started.current) / 1000)));
    form.append("language", navigator.language || "");
    try {
      const res = await fetch("/api/voice/transcribe", { method: "POST", credentials: "include", body: form });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) setError(typeof data.detail === "string" ? data.detail : "Couldn't turn that into text.");
      else if (data.text) onText(data.text);
    } catch {
      setError("Couldn't reach the server. Try again.");
    }
    setState("idle");
  }

  if (!enabled) return null;
  const label = state === "recording" ? "Stop and transcribe" : state === "working" ? "Transcribing…" : "Speak your message";
  return (
    <>
      <button
        className={`attach-icon voice-btn${state === "recording" ? " recording" : ""}`}
        type="button"
        aria-label={label}
        title={error || label}
        disabled={disabled || state === "working"}
        onClick={() => (state === "recording" ? stop() : void start())}
      >
        {state === "working" ? (
          <span className="voice-dots" aria-hidden>…</span>
        ) : (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
            <rect x="9" y="3.5" width="6" height="11" rx="3" stroke="currentColor" strokeWidth="1.8" />
            <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
        )}
      </button>
      {error ? <span className="voice-error" role="status">{error}</span> : null}
    </>
  );
}
