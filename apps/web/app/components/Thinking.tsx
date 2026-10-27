"use client";

/**
 * Live status for an in-flight chat turn.
 *
 * Lines come from the API (`GET /api/chat/progress/{id}`), which narrates the agent's
 * real steps: the connector and tool it is calling, what it searched for, which
 * teammate it asked, and so on. Before the first step arrives we show what the agent
 * is starting on (the user's own request), not a canned phrase.
 */
export function Thinking({ steps = [], hint = "" }: { steps?: string[]; hint?: string }) {
  const clean = steps.filter((s) => s && s !== "Done");
  const brief = hint.replace(/\s+/g, " ").trim();
  const start = brief ? `Starting on “${brief.length > 60 ? `${brief.slice(0, 59)}…` : brief}”` : "Starting";
  const current = clean.length ? clean[clean.length - 1] : start;
  const trail = clean.slice(-4, -1);
  return (
    <div className="turn assistant" aria-live="polite" aria-label={`Assistant is working: ${current}`}>
      {trail.length ? (
        <ul className="think-trail">
          {trail.map((s, i) => (
            <li key={`${i}-${s}`}>{s}</li>
          ))}
        </ul>
      ) : null}
      <div className="think">
        <span className="think-orb" />
        <span className="think-copy" key={current}>
          {current}
        </span>
      </div>
      {clean.length ? null : (
        <div className="think-bars">
          <div className="bar w1" />
          <div className="bar w2" />
          <div className="bar w3" />
        </div>
      )}
    </div>
  );
}
