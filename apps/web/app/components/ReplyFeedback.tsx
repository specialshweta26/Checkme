"use client";

import { useState } from "react";
import { QUALITY_REASONS, rateReply } from "../lib";

// Thumbs up / down under an agent's reply (docs/QUALITY.md). A thumbs-down asks what went wrong, so it can become a
// test case; clicking the same thumb again clears the rating.

function Thumb({ down }: { down?: boolean }) {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden style={down ? { transform: "rotate(180deg)" } : undefined}>
      <path d="M7 10v11H4a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1h3Zm0 0 4-7a2.2 2.2 0 0 1 2.9 2.6L13 10h5.6a2 2 0 0 1 2 2.4l-1.5 7A2 2 0 0 1 17.1 21H7" />
    </svg>
  );
}

export function ReplyFeedback({
  sessionId,
  requestId,
  agentId,
  rating,
  onRated,
}: {
  sessionId: string;
  requestId: string;
  agentId?: string | null;
  rating: number;
  onRated: (rating: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [reasons, setReasons] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [thanks, setThanks] = useState("");

  async function send(next: number, why: string[] = [], text = "") {
    setBusy(true);
    setError("");
    try {
      const out = await rateReply({ session_id: sessionId, request_id: requestId, rating: next, reasons: why, note: text, agent_id: agentId || "" });
      onRated(next);
      setOpen(false);
      setThanks(
        next === 1
          ? "Thanks!"
          : next === -1
            ? out.lesson
              ? "Thanks — a lesson for next time is being drafted. Review it on the Quality page."
              : "Thanks — this helps your agent improve."
            : "",
      );
      if (next !== 0) window.setTimeout(() => setThanks(""), next === -1 && out.lesson ? 6000 : 2600);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="reply-feedback">
      <div className="reply-feedback-row">
        <button
          type="button"
          className={`reply-thumb${rating === 1 ? " on up" : ""}`}
          aria-label={rating === 1 ? "Rated good (click to clear)" : "Good reply"}
          aria-pressed={rating === 1}
          title="Good reply"
          disabled={busy}
          onClick={() => send(rating === 1 ? 0 : 1)}
        >
          <Thumb />
        </button>
        <button
          type="button"
          className={`reply-thumb${rating === -1 ? " on down" : ""}${open ? " open" : ""}`}
          aria-label={rating === -1 ? "Rated bad (click to clear)" : "Bad reply"}
          aria-pressed={rating === -1}
          aria-expanded={open}
          title="Something went wrong"
          disabled={busy}
          onClick={() => (rating === -1 ? send(0) : setOpen((v) => !v))}
        >
          <Thumb down />
        </button>
        {thanks ? (
          <span className="reply-thanks">
            {thanks.includes("Quality page") ? (
              <>
                Thanks — a lesson for next time is being drafted. <a href="/quality">Review it</a>
              </>
            ) : (
              thanks
            )}
          </span>
        ) : null}
        {error ? <span className="err reply-thanks">{error}</span> : null}
      </div>
      {open ? (
        <form
          className="reply-why"
          onSubmit={(e) => {
            e.preventDefault();
            send(-1, reasons, note);
          }}
        >
          <strong>What went wrong?</strong>
          <div className="reply-why-chips">
            {QUALITY_REASONS.map((r) => (
              <button
                key={r.key}
                type="button"
                className={`chip${reasons.includes(r.key) ? " on" : ""}`}
                aria-pressed={reasons.includes(r.key)}
                onClick={() => setReasons((cur) => (cur.includes(r.key) ? cur.filter((x) => x !== r.key) : [...cur, r.key]))}
              >
                {r.label}
              </button>
            ))}
          </div>
          <textarea rows={2} placeholder="What should it have done? (optional)" value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} />
          <div className="row">
            <button className="btn small" disabled={busy}>
              {busy ? "Saving…" : "Send feedback"}
            </button>
            <button className="btn secondary small" type="button" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
    </div>
  );
}
