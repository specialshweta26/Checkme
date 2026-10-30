"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** Microphone narration while teaching a skill.
 *  - Starts only from a tap (iOS Safari needs a user gesture and HTTPS for getUserMedia).
 *  - Records webm/opus where supported (Chrome, Android, Firefox) and audio/mp4 on iOS Safari.
 *  - Each on/off stretch becomes one clip stamped with its start offset (ms since teaching began), so the
 *    server can line the transcript up with the recorded clicks and typing. */
export type NarrationClip = { blob: Blob; offsetMs: number };

const MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4;codecs=mp4a.40.2", "audio/mp4", "audio/aac", "audio/ogg;codecs=opus"];
// Keep each clip under the API's 24 MB limit; at ~48 kbps ten minutes is about 3.6 MB.
const MAX_CLIP_BYTES = 22 * 1024 * 1024;

function pickMime() {
  if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") return "";
  return MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

export function narrationSupported() {
  return typeof window !== "undefined" && Boolean(navigator.mediaDevices?.getUserMedia) && typeof MediaRecorder !== "undefined";
}

function explain(err: any) {
  const name = String(err?.name || "");
  if (name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError") {
    return "Microphone access was blocked, so teaching continues without audio. To allow it on iPhone: tap aA in the address bar › Website Settings › Microphone › Allow (or Settings › Apps › Safari › Microphone).";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") return "No microphone was found. Teaching continues without audio.";
  if (name === "NotReadableError") return "The microphone is busy in another app. Teaching continues without audio.";
  return `Could not start the microphone (${err?.message || name || "unknown error"}). Teaching continues without audio.`;
}

export function useNarration(startedAt: number) {
  const [on, setOn] = useState(false);
  const [error, setError] = useState("");
  const [seconds, setSeconds] = useState(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const chunks = useRef<Blob[]>([]);
  const bytes = useRef(0);
  const clipStart = useRef(0);
  const clips = useRef<NarrationClip[]>([]);
  const stopping = useRef<Promise<void> | null>(null);

  const release = () => {
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
  };

  const stop = useCallback((): Promise<void> => {
    const rec = recorder.current;
    if (!rec || rec.state === "inactive") {
      release();
      setOn(false);
      return stopping.current || Promise.resolve();
    }
    if (stopping.current) return stopping.current;
    stopping.current = new Promise<void>((resolve) => {
      const done = () => {
        const type = rec.mimeType || chunks.current[0]?.type || "audio/mp4";
        if (chunks.current.length) clips.current.push({ blob: new Blob(chunks.current, { type }), offsetMs: clipStart.current });
        chunks.current = [];
        bytes.current = 0;
        recorder.current = null;
        stopping.current = null;
        release();
        setOn(false);
        resolve();
      };
      rec.addEventListener("stop", done, { once: true });
      // Some Safari builds never fire "stop" if the track already ended; don't hang the save.
      window.setTimeout(() => (stopping.current ? done() : undefined), 3000);
      try {
        rec.stop();
      } catch {
        done();
      }
    });
    return stopping.current;
  }, []);

  const start = useCallback(async () => {
    setError("");
    if (!narrationSupported()) {
      setError(
        typeof window !== "undefined" && !window.isSecureContext
          ? "The microphone needs a secure (https) page."
          : "This browser cannot record audio (iPhone needs iOS 14.5 or later). Teaching continues without audio.",
      );
      return false;
    }
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      const mime = pickMime();
      const rec = new MediaRecorder(media, mime ? { mimeType: mime, audioBitsPerSecond: 48000 } : { audioBitsPerSecond: 48000 });
      stream.current = media;
      recorder.current = rec;
      chunks.current = [];
      bytes.current = 0;
      clipStart.current = Math.max(0, Date.now() - (startedAt || Date.now()));
      rec.addEventListener("dataavailable", (e: BlobEvent) => {
        if (!e.data || !e.data.size) return;
        chunks.current.push(e.data);
        bytes.current += e.data.size;
        if (bytes.current > MAX_CLIP_BYTES) {
          setError("Narration clip reached its size limit; tap the mic again to keep talking.");
          void stop();
        }
      });
      media.getAudioTracks().forEach((track) => track.addEventListener("ended", () => void stop(), { once: true }));
      rec.start(1000);
      setSeconds(0);
      setOn(true);
      return true;
    } catch (err) {
      release();
      setOn(false);
      setError(explain(err));
      return false;
    }
  }, [startedAt, stop]);

  /** Stop the mic (if on) and hand over every clip recorded so far. */
  const collect = useCallback(async () => {
    await stop();
    const out = clips.current;
    clips.current = [];
    return out;
  }, [stop]);

  const reset = useCallback(() => {
    void stop();
    clips.current = [];
    setError("");
    setSeconds(0);
  }, [stop]);

  useEffect(() => {
    if (!on) return;
    const timer = window.setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => window.clearInterval(timer);
  }, [on]);

  useEffect(
    () => () => {
      try {
        if (recorder.current && recorder.current.state !== "inactive") recorder.current.stop();
      } catch {
        /* already stopped */
      }
      release();
    },
    [],
  );

  return { on, error, seconds, start, stop, collect, reset, supported: narrationSupported() };
}
