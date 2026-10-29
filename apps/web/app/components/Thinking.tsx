"use client";

/**
 * Live status for an in-flight chat turn.
 *
 * Lines come from the API (`GET /api/chat/progress/{id}`), which narrates the agent's
 * real steps: the connector and tool it is calling, what it searched for, which
 * teammate it asked, and so on. Before the first step arrives we show what the agent
 * is starting on (the user's own request), not a canned phrase. While the model writes its
 * answer, the streamed draft shows above the status so the reply appears as it is typed.
 */
export function Thinking({
  steps = [],
  hint = "",
  draft = "",
  onStop,
  stopping = false,
  stalled = false,
}: {
  steps?: string[];
  hint?: string;
  draft?: string;
  /** Shows a Stop control on the status row. */
  onStop?: () => void;
  stopping?: boolean;
  /** No update for a while (or the connection dropped): say so, so the row never looks stuck. */
  stalled?: boolean;
}) {
  const clean = steps.filter((s) => s && s !== "Done");
  const brief = hint.replace(/\s+/g, " ").trim();
  const start = brief ? `Starting on “${brief.length > 60 ? `${brief.slice(0, 59)}…` : brief}”` : "Starting";
  const current = clean.length ? clean[clean.length - 1] : start;
  const trail = clean.slice(-4, -1);
  return (
    <div className="turn assistant" aria-live="polite" aria-label={`Lumio is working: ${current}`}>
      {trail.length ? (
        <ul className="think-trail">
          {trail.map((s, i) => (
            <li key={`${i}-${s}`}>{s}</li>
          ))}
        </ul>
      ) : null}
      {draft.trim() ? <div className="think-draft">{draft}</div> : null}
      <div className="think">
        <span className="think-orb" />
        <span className="think-copy" key={current}>
          {stopping ? "Stopping…" : current}
        </span>
        {onStop ? (
          <button type="button" className="think-stop" onClick={onStop} disabled={stopping} aria-label="Stop the agent">
            <span aria-hidden>■</span> Stop
          </button>
        ) : null}
      </div>
      {stalled && !stopping ? (
        <p className="think-stalled" role="status">
          No update for a while. It may still be working; you can stop it, or send a new message to redirect it.
        </p>
      ) : null}
      {clean.length || draft.trim() ? null : (
        <div className="think-bars">
          <div className="bar w1" />
          <div className="bar w2" />
          <div className="bar w3" />
        </div>
      )}
    </div>
  );
}
